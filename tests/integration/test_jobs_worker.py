"""Worker: lease, retry with backoff, dead-letter, contexts per job and drain (#38)."""

import threading
from datetime import timedelta

import pytest
from django.db import connections
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity
from skeleton.jobs import registry
from skeleton.jobs.models import Job
from skeleton.jobs.registry import JobType, register_job_type
from skeleton.jobs.services import enqueue
from skeleton.jobs.worker import Worker, claim, execute, reap_expired_leases
from skeleton.tenancy.context import system_context, tenant_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_job_types

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Jobs-worker-Password!58"
calls = []


def write_role(*, code, fail=False):
    from skeleton.access.models import Role
    from skeleton.tenancy.context import current_context

    Role(organization_id=current_context().organization_id, code=code, name=code).save()
    calls.append(code)
    if fail:
        raise RuntimeError("handler failure")


def as_actor(*, actor):
    calls.append(str(actor.pk))


@pytest.fixture(autouse=True)
def job_types(monkeypatch):
    calls.clear()
    with isolated_job_types():
        register_job_type(JobType("test.write", write_role, {"code": "str", "fail": "bool"}, max_attempts=2, backoff=1))
        register_job_type(JobType("test.actor", as_actor, {}, run_as="actor"))
        yield


@pytest.fixture
def tenant():
    user, organization = bootstrap_identity(
        email="worker@example.test", organization="worker-a", name="Worker A", password=PASSWORD
    )
    return user, organization


def queue(organization, code, *, fail=False, **kwargs):
    with system_context(reason="test:worker-enqueue", organization_id=organization.pk):
        return enqueue("test.write", organization_id=organization.pk, payload={"code": code, "fail": fail}, **kwargs)


def make_due():
    Job.all_objects.filter(state=Job.State.QUEUED).update(run_after=timezone.now() - timedelta(seconds=1))


def role_codes(organization):
    from skeleton.access.models import Role

    with system_context(reason="test:worker-roles", organization_id=organization.pk):
        return set(Role.objects.filter(is_builtin=False).values_list("code", flat=True))


def test_success_commits_handler_writes_with_system_job_source(tenant):
    from skeleton.audit.models import AuditEvent

    _, organization = tenant
    receipt = queue(organization, "from-job")
    [outcome] = run_pending_jobs()
    assert outcome.state == "succeeded"
    assert role_codes(organization) == {"from-job"}
    job = Job.all_objects.get(pk=receipt.id)
    assert (job.state, job.attempts, job.locked_by, job.finished_at is not None) == ("succeeded", 1, "", True)
    with system_context(reason="test:worker-audit", organization_id=organization.pk):
        sources = set(AuditEvent.objects.filter(model_label="sk_access.role").values_list("source", flat=True))
    assert "system:job:test.write" in sources


def test_failure_rolls_back_handler_retries_with_backoff_then_dead(tenant):
    _, organization = tenant
    receipt = queue(organization, "never", fail=True)
    [first] = run_pending_jobs()
    job = Job.all_objects.get(pk=receipt.id)
    assert first.state == "queued" and first.error_class == "RuntimeError"
    assert job.attempts == 1 and job.run_after > timezone.now() and job.last_error_class == "RuntimeError"
    assert run_pending_jobs() == []  # Backoff: not due yet.
    make_due()
    [second] = run_pending_jobs()
    assert second.state == "dead"
    assert Job.all_objects.get(pk=receipt.id).attempts == 2
    assert role_codes(organization) == set()


def test_actor_job_with_revoked_membership_fails_closed(tenant):
    from skeleton.tenancy.models import Membership

    user, organization = tenant
    member, _ = bootstrap_identity(
        email="worker-member@example.test", organization="worker-b", name="Worker B", password=PASSWORD
    )
    with system_context(reason="test:worker-member", organization_id=organization.pk):
        membership = Membership(organization=organization, user=member)
        membership.save()
    with tenant_context(organization_id=organization.pk, actor=member, source="test:worker-actor"):
        enqueue("test.actor", organization_id=organization.pk, payload={}, actor=member)
        enqueue("test.actor", organization_id=organization.pk, payload={}, actor=member)
    ok = run_pending_jobs(limit=1)[0]
    assert ok.state == "succeeded" and calls == [str(member.pk)]
    with system_context(reason="test:worker-revoke", organization_id=organization.pk):
        membership.refresh_from_db()
        membership.is_active = False
        membership.save(update_fields={"is_active"})
    [denied] = run_pending_jobs()
    assert denied.state == "dead" and calls == [str(member.pk)]
    assert Job.all_objects.get(pk=denied.job_id).last_error_code == "actor_unauthorized"


def test_job_of_inactive_organization_is_skipped_with_reason(tenant):
    from skeleton.tenancy.models import Organization

    _, organization = tenant
    receipt = queue(organization, "skipped")
    with system_context(reason="test:worker-deactivate"):
        org = Organization.objects.get(pk=organization.pk)
        org.is_active = False
        org.save(update_fields={"is_active"})
    [outcome] = run_pending_jobs()
    job = Job.all_objects.get(pk=receipt.id)
    assert outcome.state == "cancelled" and job.last_error_code == "organization_inactive"
    assert calls == []


def test_stop_finishes_current_job_and_claims_no_other(tenant):
    _, organization = tenant
    worker = Worker(worker_id="stop-worker")
    original = write_role

    def stopping(**payload):
        original(**payload)
        worker.stop()

    registry._registry["test.write"] = JobType("test.write", stopping, {"code": "str", "fail": "bool"})
    queue(organization, "first")
    queue(organization, "second")
    outcomes = worker.run(once=True)
    assert [outcome.state for outcome in outcomes] == ["succeeded"]
    assert Job.all_objects.filter(state=Job.State.QUEUED).count() == 1


def test_expired_lease_is_requeued_once(tenant):
    _, organization = tenant
    receipt = queue(organization, "lease")
    job = claim("crashed-worker")
    assert job.pk == receipt.id
    Job.all_objects.filter(pk=job.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
    assert reap_expired_leases() == 1
    assert reap_expired_leases() == 0
    job = Job.all_objects.get(pk=receipt.id)
    assert (job.state, job.locked_by, job.last_error_code) == ("queued", "", "lease_expired")
    # The crashed worker cannot mark the job anymore: it lost the lease.
    stale = Job.all_objects.get(pk=receipt.id)
    stale.state = Job.State.RUNNING
    assert execute(stale, "crashed-worker").state in {"queued", "dead"}
    assert role_codes(organization) == set()


def test_two_postgres_workers_never_run_the_same_job(tenant):
    _, organization = tenant
    for index in range(12):
        queue(organization, f"parallel-{index}")
    claimed = {0: [], 1: []}
    barrier = threading.Barrier(2)

    def run(slot):
        try:
            barrier.wait()
            while (job := claim(f"worker-{slot}")) is not None:
                claimed[slot].append(job.pk)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run, args=(slot,)) for slot in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not set(claimed[0]) & set(claimed[1])
    assert len(claimed[0]) + len(claimed[1]) == 12
