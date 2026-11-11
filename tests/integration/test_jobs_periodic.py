"""Periodic jobs: one job per window and organization, without leader election (#39)."""

import threading
from datetime import UTC, datetime, time, timedelta

import pytest
from django.db import connections

from skeleton.accounts.services import bootstrap_identity
from skeleton.jobs import periodic, registry
from skeleton.jobs.models import Job
from skeleton.jobs.periodic import periodic_problems, register_periodic, schedule_due
from skeleton.jobs.registry import JobType, register_job_type
from skeleton.tenancy.context import system_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Jobs-periodic-Password!36"
NOW = datetime(2026, 11, 9, 10, 7, 30, tzinfo=UTC)


def noop(**kwargs):
    return None


@pytest.fixture(autouse=True)
def declarations(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})
    monkeypatch.setattr(registry, "_duplicates", [])
    monkeypatch.setattr(periodic, "_periodic", {})
    register_job_type(JobType("test.tick", noop, {}))
    register_job_type(JobType("test.global_tick", noop, {}, scope="global"))


@pytest.fixture
def organizations():
    _, alpha = bootstrap_identity(email="tick-a@example.test", organization="tick-a", name="A", password=PASSWORD)
    _, beta = bootstrap_identity(email="tick-b@example.test", organization="tick-b", name="B", password=PASSWORD)
    return alpha, beta


def ticks(job_type="test.tick"):
    return sorted(
        (row[0], row[1])
        for row in Job.all_objects.filter(job_type=job_type).values_list("job_organization_id", "run_after")
    )


def test_window_arithmetic():
    every = periodic.PeriodicJob("x", "test.tick", every=timedelta(minutes=5))
    assert every.window_start(NOW) == datetime(2026, 11, 9, 10, 5, tzinfo=UTC)
    daily = periodic.PeriodicJob("y", "test.tick", daily_at=time(9, 0))
    assert daily.window_start(NOW) == datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    early = periodic.PeriodicJob("z", "test.tick", daily_at=time(11, 0))
    assert early.window_start(NOW) == datetime(2026, 11, 8, 11, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        register_periodic("bad", "test.tick")
    with pytest.raises(ValueError):
        register_periodic("bad", "test.tick", every=timedelta(seconds=5))


def test_one_job_per_window_and_organization_even_when_repeated(organizations):
    alpha, beta = organizations
    register_periodic("tick", "test.tick", every=timedelta(minutes=5))
    assert schedule_due(now=NOW) == 2
    assert schedule_due(now=NOW + timedelta(minutes=1)) == 0
    window = datetime(2026, 11, 9, 10, 5, tzinfo=UTC)
    assert ticks() == sorted([(alpha.pk, window), (beta.pk, window)])
    assert schedule_due(now=NOW + timedelta(minutes=5)) == 2


def test_inactive_and_deleted_organizations_receive_nothing(organizations):
    from skeleton.tenancy.models import Organization

    alpha, beta = organizations
    with system_context(reason="test:tick-deactivate"):
        org = Organization.objects.get(pk=beta.pk)
        org.is_active = False
        org.save(update_fields={"is_active"})
    register_periodic("tick", "test.tick", every=timedelta(minutes=5))
    schedule_due(now=NOW)
    assert [row[0] for row in ticks()] == [alpha.pk]


def test_schedule_change_applies_from_next_window(organizations):
    register_periodic("tick", "test.tick", every=timedelta(minutes=5))
    schedule_due(now=NOW)
    periodic._periodic.clear()
    register_periodic("tick", "test.tick", every=timedelta(minutes=15))
    assert schedule_due(now=NOW) == 2  # New cadence, new window key (10:00).
    assert {row[1] for row in ticks()} == {
        datetime(2026, 11, 9, 10, 5, tzinfo=UTC),
        datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
    }


def test_global_periodic_jobs_and_declaration_checks(organizations):
    register_periodic("global", "test.global_tick", daily_at=time(3, 0), per_organization=False)
    register_periodic("mismatch", "test.tick", daily_at=time(3, 0), per_organization=False)
    register_periodic("unknown", "test.missing", every=timedelta(hours=1))
    problems = "\n".join(periodic_problems())
    assert "mismatch: per_organization" in problems and "unknown: unknown job type" in problems
    periodic._periodic.pop("mismatch")
    periodic._periodic.pop("unknown")
    assert schedule_due(now=NOW) == 1
    assert ticks("test.global_tick") == [(None, datetime(2026, 11, 9, 3, 0, tzinfo=UTC))]


def test_two_concurrent_schedulers_produce_one_job_per_window(organizations):
    register_periodic("tick", "test.tick", every=timedelta(minutes=5))
    barrier = threading.Barrier(2)
    errors = []

    def run():
        try:
            barrier.wait()
            schedule_due(now=NOW)
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
    assert len(ticks()) == 2
