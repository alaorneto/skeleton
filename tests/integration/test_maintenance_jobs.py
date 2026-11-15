"""Jobs during the administrative maintenance mode (#448), in PostgreSQL as the runtime role."""

import io
import signal
from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity
from skeleton.instance import maintenance
from skeleton.instance.maintenance import enter, leave
from skeleton.instance.store import InstanceStore
from skeleton.jobs.models import Job, WorkerHeartbeat
from skeleton.jobs.periodic import register_periodic
from skeleton.jobs.registry import JobType, register_job_type
from skeleton.testing.registries import isolated_job_types

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Quiet-harbor-Phrase-448!"
RAN = []


def record(**kwargs):
    RAN.append(kwargs)


@pytest.fixture(autouse=True)
def declarations(monkeypatch):
    with isolated_job_types():
        monkeypatch.setattr(signal, "signal", lambda *args: None)  # Keep pytest's own handlers.
        register_job_type(JobType("test.maintenance_tick", record, {}))
        RAN.clear()
        yield


@pytest.fixture
def store(tmp_path, settings):
    path = tmp_path / "instance" / "instance.sqlite3"
    settings.SKELETON_MAINTENANCE_STORE = str(path)
    settings.SKELETON_MAINTENANCE_MARKER = None
    yield InstanceStore(path)
    maintenance._readers.pop(str(path), None)


@pytest.fixture
def organization():
    _, organization = bootstrap_identity(
        email="ops-queue@example.test", organization="queue-org", name="Queue", password=PASSWORD
    )
    return organization


def _switch(store, *, active, pause_jobs=True):
    if active:
        enter(store, message="", ends_at=None, pause_jobs=pause_jobs, source="test")
    else:
        leave(store, source="test")
    maintenance.reader_for(store.path)._cached = None  # Do not wait the 5 s of the per-process cache.


def _enqueue(organization):
    from skeleton.jobs.services import enqueue
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:maintenance-enqueue", organization_id=organization.pk):
        return enqueue("test.maintenance_tick", organization_id=organization.pk).id


def _state(organization, job_id):
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:maintenance-state", organization_id=organization.pk):
        return Job.objects.get(pk=job_id).state


def _worker_once():
    out = io.StringIO()
    call_command("run_worker", "--once", "--worker-id", "maintenance-worker", stdout=out)
    return out.getvalue().strip()


def test_worker_pauses_only_when_the_mode_pauses_jobs(store, organization, monkeypatch):
    from skeleton.jobs import worker

    beats = []
    original = worker.heartbeat
    monkeypatch.setattr(worker, "heartbeat", lambda *a, **k: beats.append(k.get("state")) or original(*a, **k))
    job_id = _enqueue(organization)
    _switch(store, active=True, pause_jobs=True)
    assert _worker_once() == "Jobs processados: 0."
    assert RAN == [] and _state(organization, job_id) == "queued"
    assert "paused" in beats  # --once then exits and records "stopped", as with the deployment marker.
    assert WorkerHeartbeat.all_objects.filter(worker_id="maintenance-worker").exists()

    _switch(store, active=True, pause_jobs=False)  # --keep-jobs: the worker goes on.
    assert _worker_once() == "Jobs processados: 1."
    assert _state(organization, job_id) == "succeeded"

    second = _enqueue(organization)
    _switch(store, active=True, pause_jobs=True)
    assert _worker_once() == "Jobs processados: 0."
    _switch(store, active=False)  # Leaving the mode resumes the queue.
    assert _worker_once() == "Jobs processados: 1."
    assert _state(organization, second) == "succeeded"


def test_scheduler_does_not_schedule_while_the_mode_pauses_jobs(store, organization):
    register_periodic("maintenance-tick", "test.maintenance_tick", every=timedelta(minutes=5))
    _switch(store, active=True, pause_jobs=True)
    out = io.StringIO()
    call_command("run_scheduler", "--once", stdout=out)
    assert out.getvalue().strip() == "Jobs periódicos criados: 0."
    assert not Job.all_objects.filter(job_type="test.maintenance_tick").exists()
    _switch(store, active=True, pause_jobs=False)
    out = io.StringIO()
    call_command("run_scheduler", "--once", stdout=out)
    assert out.getvalue().strip() == "Jobs periódicos criados: 1."


def test_check_jobs_does_not_report_lag_during_the_pause(store, organization):
    job_id = _enqueue(organization)
    Job.all_objects.filter(pk=job_id).update(run_after=timezone.now() - timedelta(minutes=30))
    with pytest.raises(CommandError, match="queue_lag"):
        call_command("check_jobs", "--max-lag", "1", stdout=io.StringIO())
    _switch(store, active=True, pause_jobs=True)
    out = io.StringIO()
    call_command("check_jobs", "--max-lag", "1", stdout=out)
    assert "paused=1" in out.getvalue()
    _switch(store, active=True, pause_jobs=False)  # Jobs keep running: a late queue is a real lag again.
    with pytest.raises(CommandError, match="queue_lag"):
        call_command("check_jobs", "--max-lag", "1", stdout=io.StringIO())


def test_the_deployment_marker_pauses_regardless_of_pause_jobs(store, organization, tmp_path, settings):
    marker = tmp_path / "maintenance"
    marker.touch()
    settings.SKELETON_MAINTENANCE_MARKER = str(marker)
    job_id = _enqueue(organization)
    _switch(store, active=True, pause_jobs=False)
    assert _worker_once() == "Jobs processados: 0."
    assert _state(organization, job_id) == "queued"
