"""Database job queue: enqueue in the business transaction with typed payloads (#37).

Runs on PostgreSQL with the runtime role (INSERT guard).
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction

from skeleton.accounts.services import bootstrap_identity
from skeleton.jobs.models import Job
from skeleton.jobs.registry import JobType, job_type_problems, register_job_type
from skeleton.jobs.services import enqueue
from skeleton.tenancy.context import system_context, tenant_context
from skeleton.testing.registries import isolated_job_types

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Jobs-queue-Password!47"


def handler(**kwargs):
    return None


@pytest.fixture(autouse=True)
def job_types(monkeypatch):
    with isolated_job_types():
        register_job_type(JobType("test.ping", handler, {"record_id": "uuid", "count": "int"}))
        register_job_type(JobType("test.as_actor", handler, {}, run_as="actor"))
        register_job_type(JobType("test.global", handler, {}, scope="global"))
        yield


@pytest.fixture
def tenants():
    alice, alpha = bootstrap_identity(email="jobs-a@example.test", organization="jobs-a", name="A", password=PASSWORD)
    bob, beta = bootstrap_identity(email="jobs-b@example.test", organization="jobs-b", name="B", password=PASSWORD)
    return alice, alpha, bob, beta


def jobs():
    return list(Job.all_objects.order_by("created_at").values_list("job_type", "job_organization_id", "state"))


def test_rolled_back_enqueue_leaves_no_job_and_savepoint_keeps_the_rest(tenants):
    alice, alpha, _, _ = tenants
    payload = {"record_id": str(alice.pk), "count": 1}
    with pytest.raises(RuntimeError), tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs"):
        enqueue("test.ping", organization_id=alpha.pk, payload=payload)
        raise RuntimeError("business failure")
    assert jobs() == []
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs-savepoint"):
        kept = enqueue("test.ping", organization_id=alpha.pk, payload=payload)
        try:
            with transaction.atomic():
                enqueue("test.ping", organization_id=alpha.pk, payload=payload)
                raise RuntimeError("inner failure")
        except RuntimeError:
            pass
    assert [row[0] for row in jobs()] == ["test.ping"]
    assert kept.created and kept.state == "queued" and kept.status == "queued"


@pytest.mark.parametrize(
    "code,payload",
    [
        ("unknown", {}),
        ("test.ping", {"record_id": "not-a-uuid", "count": 1}),
        ("test.ping", {"record_id": "00000000-0000-0000-0000-000000000001", "count": True}),
        ("test.ping", {"record_id": "00000000-0000-0000-0000-000000000001", "count": 1, "extra": 1}),
        ("test.ping", {"record_id": "00000000-0000-0000-0000-000000000001"}),
    ],
)
def test_invalid_type_or_payload_is_refused(tenants, code, payload):
    alice, alpha, _, _ = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs-invalid"):
        with pytest.raises(ValidationError):
            enqueue(code, organization_id=alpha.pk, payload=payload)


def test_payload_above_four_kilobytes_is_refused(tenants, monkeypatch):
    alice, alpha, _, _ = tenants
    register_job_type(JobType("test.wide", handler, {f"k{i}": "str" for i in range(30)}))
    payload = {f"k{i}": "x" * 200 for i in range(30)}
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs-wide"):
        with pytest.raises(ValidationError):
            enqueue("test.wide", organization_id=alpha.pk, payload=payload)


def test_tenant_a_cannot_enqueue_for_b_in_python_and_database(tenants):
    alice, alpha, _, beta = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs-cross"):
        with pytest.raises(ValidationError):
            enqueue("test.ping", organization_id=beta.pk, payload={"record_id": str(alice.pk), "count": 1})
        with pytest.raises(ValidationError):
            enqueue("test.global", payload={})
        from django.db import ProgrammingError

        job = Job(job_type="test.ping", job_organization_id=beta.pk, payload={}, run_after=alice.created_at)
        with pytest.raises(ProgrammingError), transaction.atomic():
            job.save()
    assert jobs() == []


def test_idempotency_key_returns_the_existing_job(tenants):
    alice, alpha, bob, beta = tenants
    payload = {"record_id": str(alice.pk), "count": 1}
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs-idempotent"):
        first = enqueue("test.ping", organization_id=alpha.pk, payload=payload, idempotency_key="k1")
        second = enqueue("test.ping", organization_id=alpha.pk, payload=payload, idempotency_key="k1")
    assert first.id == second.id and first.created and not second.created
    with tenant_context(organization_id=beta.pk, actor=bob, source="test:jobs-idempotent-b"):
        other = enqueue("test.ping", organization_id=beta.pk, payload=payload, idempotency_key="k1")
    # The key is unique per organization (#241): B gets its own job, never A's identity.
    assert other.created and other.id != first.id
    assert len(jobs()) == 2


def test_actor_jobs_require_an_organization_and_an_actor(tenants):
    alice, alpha, _, _ = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs-actor"):
        with pytest.raises(ValidationError):
            enqueue("test.as_actor", organization_id=alpha.pk, payload={})
        receipt = enqueue("test.as_actor", organization_id=alpha.pk, payload={}, actor=alice)
    assert Job.all_objects.get(pk=receipt.id).actor_id == alice.pk
    with system_context(reason="test:jobs-global"):
        enqueue("test.global", payload={})
        with pytest.raises(ValidationError):
            enqueue("test.ping", payload={"record_id": str(alice.pk), "count": 1})


def test_registry_checks_report_duplicates_and_bad_declarations():
    register_job_type(JobType("test.ping", lambda: None, {}))
    register_job_type(JobType("test.bad", "not callable", {"x": "float"}, run_as="root"))
    problems = "\n".join(job_type_problems())
    assert "test.ping: duplicate" in problems
    assert "test.bad: handler is not callable" in problems
    assert "test.bad: run_as" in problems
    assert "test.bad: payload types" in problems


def test_same_key_in_two_organizations_creates_two_jobs(tenants):
    """A key chosen by A never blocks B nor hands B the job of A (#241)."""
    alice, alpha, bob, beta = tenants
    payload = {"record_id": str(alice.pk), "count": 1}
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs-key-a"):
        in_a = enqueue("test.ping", organization_id=alpha.pk, payload=payload, idempotency_key="shared")
    with tenant_context(organization_id=beta.pk, actor=bob, source="test:jobs-key-b"):
        in_b = enqueue("test.ping", organization_id=beta.pk, payload=payload, idempotency_key="shared")
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs-key-a-again"):
        again = enqueue("test.ping", organization_id=alpha.pk, payload=payload, idempotency_key="shared")
    assert in_a.created and in_b.created and in_a.id != in_b.id
    assert again.id == in_a.id and not again.created
    owners = dict(Job.all_objects.filter(idempotency_key="shared").values_list("pk", "job_organization_id"))
    assert owners == {in_a.id: alpha.pk, in_b.id: beta.pk}


def test_global_jobs_keep_one_job_per_key(tenants):
    with system_context(reason="test:jobs-global-key"):
        first = enqueue("test.global", payload={}, idempotency_key="global-key")
        second = enqueue("test.global", payload={}, idempotency_key="global-key")
    assert first.id == second.id and first.created and not second.created


def test_enqueue_with_key_reraises_foreign_integrity_errors(tenants, monkeypatch):
    """Only the idempotency constraints mean "already queued"; any other violation propagates."""
    from django.db import IntegrityError

    alice, alpha, _, _ = tenants

    def broken_save(self, *args, **kwargs):
        raise IntegrityError("some other constraint")

    monkeypatch.setattr(Job, "save", broken_save)
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs-foreign-integrity"):
        with pytest.raises(IntegrityError):
            enqueue(
                "test.ping",
                organization_id=alpha.pk,
                payload={"record_id": str(alice.pk), "count": 1},
                idempotency_key="k-foreign",
            )


def test_two_connections_enqueue_the_same_key_in_one_organization(tenants):
    """Two workers race on one key: one job, one created and one found (#241)."""
    import threading

    from django.db import connections

    alice, alpha, _, _ = tenants
    barrier = threading.Barrier(2)
    receipts, errors = [], []

    def run():
        try:
            barrier.wait()
            with tenant_context(organization_id=alpha.pk, actor=alice, source="test:jobs-race"):
                receipts.append(
                    enqueue(
                        "test.ping",
                        organization_id=alpha.pk,
                        payload={"record_id": str(alice.pk), "count": 1},
                        idempotency_key="race",
                    )
                )
        except Exception as error:  # noqa: BLE001
            errors.append(error)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(receipt.created for receipt in receipts) == [False, True]
    assert len({receipt.id for receipt in receipts}) == 1
