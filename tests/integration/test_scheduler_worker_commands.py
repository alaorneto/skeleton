"""run_scheduler and run_worker in their single-pass mode (#335)."""

import io
import signal
from datetime import timedelta

import pytest
from django.core.management import call_command

from skeleton.accounts.services import bootstrap_identity
from skeleton.jobs import periodic, registry
from skeleton.jobs.models import Job
from skeleton.jobs.periodic import register_periodic
from skeleton.jobs.registry import JobType, register_job_type

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Scheduler-command-Phrase-335!"
RAN = []


def record(**kwargs):
    RAN.append(kwargs)


@pytest.fixture(autouse=True)
def declarations(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})
    monkeypatch.setattr(registry, "_duplicates", [])
    monkeypatch.setattr(periodic, "_periodic", {})
    # The commands install process-wide SIGTERM/SIGINT handlers; keep pytest's own.
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    register_job_type(JobType("test.command_tick", record, {}))
    RAN.clear()


@pytest.fixture
def organizations():
    _, alpha = bootstrap_identity(email="cmd-a@example.test", organization="cmd-a", name="A", password=PASSWORD)
    _, beta = bootstrap_identity(email="cmd-b@example.test", organization="cmd-b", name="B", password=PASSWORD)
    return alpha, beta


def scheduled():
    return Job.all_objects.filter(job_type="test.command_tick").count()


def test_run_scheduler_once_enqueues_the_current_window(organizations):
    register_periodic("command-tick", "test.command_tick", every=timedelta(minutes=5))
    out = io.StringIO()
    call_command("run_scheduler", "--once", stdout=out)
    assert scheduled() == 2
    assert out.getvalue().strip() == "Jobs periódicos criados: 2."


def test_run_scheduler_once_does_nothing_while_maintenance_is_paused(monkeypatch, organizations):
    register_periodic("command-tick", "test.command_tick", every=timedelta(minutes=5))
    monkeypatch.setattr("skeleton.jobs.management.commands.run_scheduler.maintenance_paused", lambda: True)
    out = io.StringIO()
    call_command("run_scheduler", "--once", stdout=out)
    assert out.getvalue().strip() == "Jobs periódicos criados: 0."
    assert scheduled() == 0


def test_run_worker_once_runs_a_queued_job(organizations):
    from skeleton.jobs.services import enqueue
    from skeleton.tenancy.context import system_context

    alpha, _ = organizations
    with system_context(reason="test:command-enqueue", organization_id=alpha.pk):
        receipt = enqueue("test.command_tick", organization_id=alpha.pk)
    out = io.StringIO()
    call_command("run_worker", "--once", stdout=out)
    assert out.getvalue().strip() == "Jobs processados: 1."
    assert len(RAN) == 1
    with system_context(reason="test:command-state", organization_id=alpha.pk):
        assert Job.objects.get(pk=receipt.id).state == "succeeded"
