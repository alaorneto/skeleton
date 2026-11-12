"""Operating the queue (#40): /platform/jobs/, CAS on dead jobs, retention and check_jobs."""

import logging
from datetime import timedelta
from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.jobs.models import Job, WorkerHeartbeat
from skeleton.jobs.operations import discard_dead_job, queue_health, resend_dead_job
from skeleton.jobs.registry import JobType, register_job_type
from skeleton.jobs.retention import RETENTION_JOB, expired, purge_expired_jobs
from skeleton.jobs.services import enqueue
from skeleton.tenancy.context import system_context
from skeleton.testing.registries import isolated_job_types

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Jobs-platform-Password!40"
SECRET_PAYLOAD = "payload-value-never-shown"


def noop(**kwargs):
    return None


@pytest.fixture(autouse=True)
def job_types(monkeypatch):
    with isolated_job_types(keep_existing=True):
        register_job_type(JobType("test.ops", noop, {"note": "str"}, max_attempts=2))
        yield


def login(email, password=PASSWORD):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": email, "password": password, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


@pytest.fixture
def platform():
    user, organization = bootstrap_identity(
        email="jobs-platform@example.test", organization="jobs-platform", name="Jobs", password=PASSWORD
    )
    return user, organization


def make_job(organization, *, state="dead", finished_days=None, note=SECRET_PAYLOAD):
    with system_context(reason="test:jobs-ops", organization_id=organization.pk):
        receipt = enqueue("test.ops", organization_id=organization.pk, payload={"note": note})
    finished = timezone.now() - timedelta(days=finished_days) if finished_days is not None else None
    Job.all_objects.filter(pk=receipt.id).update(
        state=state, attempts=2 if state == "dead" else 0, finished_at=finished, last_error_code="boom"
    )
    return Job.all_objects.get(pk=receipt.id)


def post(client, job, action, *, updated_at=None):
    return client.post(
        reverse("platform:job-action", args=[job.pk]),
        {
            "action": action,
            "expected_updated_at": (updated_at or job.updated_at).isoformat(),
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )


def test_list_shows_states_and_never_the_payload(platform):
    user, organization = platform
    job = make_job(organization)
    client = login(user.email)
    html = client.get(reverse("platform:jobs")).content.decode()
    assert "test.ops" in html and "boom" in html and organization.name in html
    assert SECRET_PAYLOAD not in html
    assert reverse("platform:job-action", args=[job.pk]) in html
    filtered = client.get(reverse("platform:jobs"), {"state": "queued"}).content.decode()
    assert reverse("platform:job-action", args=[job.pk]) not in filtered
    assert client.get(reverse("platform:jobs"), {"state": "nope"}).status_code == 200  # Invalid filter: ignored.


def test_only_platform_administrators_reach_the_page(platform):
    _, organization = platform
    job = make_job(organization)
    assert Client().get(reverse("platform:jobs")).status_code == 302  # Anonymous: login.
    from allauth.account.models import EmailAddress

    from skeleton.accounts.services import create_identity

    with system_context(reason="test:jobs-outsider"):
        outsider = create_identity(email="jobs-outsider@example.test", password=PASSWORD)
        EmailAddress.objects.create(user=outsider, email=outsider.email, verified=True, primary=True)
    client = login(outsider.email)
    assert client.get(reverse("platform:jobs")).status_code == 403
    assert post(client, job, "resend").status_code == 403
    job.refresh_from_db()
    assert job.state == "dead" and job.attempts == 2


def test_get_never_changes_state_and_post_needs_csrf(platform):
    user, organization = platform
    job = make_job(organization)
    client = login(user.email)
    assert client.get(reverse("platform:job-action", args=[job.pk])).status_code == 405
    raw = client.post(
        reverse("platform:job-action", args=[job.pk]),
        {"action": "resend", "expected_updated_at": job.updated_at.isoformat()},
    )
    assert raw.status_code == 403  # CSRF.
    job.refresh_from_db()
    assert job.state == "dead"


def test_resend_resets_attempts_exactly_once(platform, caplog):
    user, organization = platform
    job = make_job(organization)
    client = login(user.email)
    with caplog.at_level(logging.INFO, logger="skeleton.security"):
        assert post(client, job, "resend").status_code == 302
    job_after = Job.all_objects.get(pk=job.pk)
    assert (job_after.state, job_after.attempts, job_after.last_error_code) == ("queued", 0, "")
    assert job_after.run_after <= timezone.now()
    assert "job.resent" in [getattr(record, "event", None) for record in caplog.records]
    # The same form submitted again (stale updated_at, no longer dead) changes nothing.
    Job.all_objects.filter(pk=job.pk).update(attempts=1)
    response = post(client, job, "resend")
    assert response.status_code == 302
    assert Job.all_objects.get(pk=job.pk).attempts == 1
    with pytest.raises(ConcurrentUpdate):
        resend_dead_job(job.pk, expected_updated_at=job.updated_at, actor=user)


def test_concurrent_change_wins_over_a_stale_resend(platform):
    user, organization = platform
    job = make_job(organization)
    stale = job.updated_at
    Job.all_objects.filter(pk=job.pk).update(last_error_code="changed")  # Bumps updated_at, still dead.
    with pytest.raises(ConcurrentUpdate):
        resend_dead_job(job.pk, expected_updated_at=stale, actor=user)
    with pytest.raises(ConcurrentUpdate):
        resend_dead_job(uuid4(), expected_updated_at=stale, actor=user)
    assert Job.all_objects.get(pk=job.pk).state == "dead"


def test_discard_marks_cancelled_and_queued_jobs_are_not_actionable(platform):
    user, organization = platform
    job = make_job(organization)
    client = login(user.email)
    assert post(client, job, "discard").status_code == 302
    job.refresh_from_db()
    assert (job.state, job.last_error_code) == ("cancelled", "discarded") and job.finished_at is not None
    queued = make_job(organization, state="queued")
    with pytest.raises(ConcurrentUpdate):
        discard_dead_job(queued.pk, expected_updated_at=queued.updated_at, actor=user)


def test_actions_need_a_recent_authentication(platform, settings):
    user, organization = platform
    job = make_job(organization)
    client = login(user.email)
    settings.ACCOUNT_REAUTHENTICATION_TIMEOUT = 0
    response = post(client, job, "resend")
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    job.refresh_from_db()
    assert job.state == "dead"


def test_retention_deletes_only_terminal_states_past_their_window(platform):
    _, organization = platform
    old_success = make_job(organization, state="succeeded", finished_days=8)
    new_success = make_job(organization, state="succeeded", finished_days=6)
    old_cancelled = make_job(organization, state="cancelled", finished_days=8)
    dead_in_window = make_job(organization, state="dead", finished_days=8)
    old_dead = make_job(organization, state="dead", finished_days=31)
    old_queued = make_job(organization, state="queued")
    Job.all_objects.filter(pk=old_queued.pk).update(run_after=timezone.now() - timedelta(days=90))
    running = make_job(organization, state="running", finished_days=90)
    selected = set(expired()["jobs"].values_list("pk", flat=True))
    assert selected == {old_success.pk, old_cancelled.pk, old_dead.pk}
    now = timezone.now()
    WorkerHeartbeat(worker_id="gone", state="stopped", last_seen=now - timedelta(days=40)).save()
    WorkerHeartbeat(worker_id="alive", state="running", last_seen=now - timedelta(days=40)).save()
    # The real path: the registered global job, run by the worker as the runtime role.
    from skeleton.testing.jobs import run_pending_jobs

    with system_context(reason="test:jobs-retention"):
        enqueue(RETENTION_JOB, idempotency_key="test-retention")
    outcomes = run_pending_jobs()
    assert [outcome.state for outcome in outcomes if outcome.job_type == RETENTION_JOB] == ["succeeded"]
    remaining = set(Job.all_objects.values_list("pk", flat=True))
    assert {new_success.pk, dead_in_window.pk, old_queued.pk, running.pk} <= remaining
    assert not {old_success.pk, old_cancelled.pk, old_dead.pk} & remaining
    assert set(WorkerHeartbeat.all_objects.values_list("worker_id", flat=True)) == {"alive"}
    assert purge_expired_jobs() == {"jobs": 0, "heartbeats": 0}


def test_check_jobs_reports_lag_dead_and_workers(platform):
    _, organization = platform
    out = StringIO()
    call_command("check_jobs", stdout=out)
    assert "due=0" in out.getvalue()
    with pytest.raises(CommandError, match="no_live_worker"):
        call_command("check_jobs", "--require-worker", stdout=StringIO())
    late = make_job(organization, state="queued")
    Job.all_objects.filter(pk=late.pk).update(run_after=timezone.now() - timedelta(minutes=10))
    make_job(organization, state="dead")
    with pytest.raises(CommandError, match="queue_lag"):
        call_command("check_jobs", "--max-lag", "60", stdout=StringIO())
    with pytest.raises(CommandError, match="dead_jobs"):
        call_command("check_jobs", "--max-lag", "3600", "--max-dead", "0", stdout=StringIO())
    WorkerHeartbeat(worker_id="probe", state="running", last_seen=timezone.now()).save()
    health = queue_health()
    assert (health.due, health.dead, health.live_workers) == (1, 1, 1) and health.oldest_due_lag_s >= 590
    call_command("check_jobs", "--max-lag", "3600", "--require-worker", stdout=StringIO())
