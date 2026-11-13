"""Jobs of an app never run where the app is off (#337): scheduling skips it, the worker cancels it.

Runs on PostgreSQL with the runtime role, with job types owned by the example app.
"""

from datetime import UTC, datetime, timedelta

import pytest

from examples.notes.models import Note
from skeleton.jobs.models import Job
from skeleton.jobs.periodic import register_periodic, schedule_due
from skeleton.jobs.registry import JobType, register_job_type
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_job_types
from skeleton.testing.service_api import build_service_scenario

pytestmark = pytest.mark.needs_example_app
NOW = datetime(2026, 11, 9, 10, 7, 30, tzinfo=UTC)
CALLS = []


def handler(**kwargs):
    from skeleton.tenancy.context import current_context

    CALLS.append(current_context().organization_id)


@pytest.fixture
def scenario(skeleton_db):
    CALLS.clear()
    with isolated_job_types(keep_existing=True):
        register_job_type(JobType("test.notes_tick", handler, {}, owner_app="example_notes"))
        register_job_type(JobType("test.core_tick", handler, {}))
        register_job_type(JobType("test.subsystem_tick", handler, {}, owner_app="sk_notifications"))
        register_periodic("notes-tick", "test.notes_tick", every=timedelta(minutes=5))
        yield build_service_scenario(Note, slug="job-owner")


def scheduled(job_type, organization):
    with system_context(reason="test:job-owner", organization_id=organization.pk):
        rows = Job.objects.filter(job_type=job_type, job_organization_id=organization.pk)
        return list(rows.values_list("state", "last_error_code"))


def enqueue_in(job_type, organization):
    from skeleton.jobs.services import enqueue

    with system_context(reason="test:job-owner-enqueue", organization_id=organization.pk):
        enqueue(job_type, organization_id=organization.pk, payload={})


def test_periodic_job_is_not_scheduled_where_the_app_is_disabled(scenario):
    with scenario.application_disabled(scenario.a):
        schedule_due(now=NOW)
    assert scheduled("test.notes_tick", scenario.a) == []
    assert len(scheduled("test.notes_tick", scenario.b)) == 1


def test_queued_job_is_cancelled_when_the_app_was_disabled(scenario):
    enqueue_in("test.notes_tick", scenario.a)
    with scenario.application_disabled(scenario.a):
        run_pending_jobs()
    assert scheduled("test.notes_tick", scenario.a) == [("cancelled", "application_disabled")]
    assert CALLS == []


def test_job_runs_again_after_the_app_is_enabled(scenario):
    with scenario.application_disabled(scenario.a):
        schedule_due(now=NOW)
    schedule_due(now=NOW)  # Same window, app enabled again: the organization is scheduled now.
    assert len(scheduled("test.notes_tick", scenario.a)) == 1
    with system_context(reason="test:job-owner-due"):
        Job.all_objects.filter(job_type="test.notes_tick").update(run_after=NOW - timedelta(minutes=1))
    run_pending_jobs()
    assert scenario.a.pk in CALLS
    assert scheduled("test.notes_tick", scenario.a) == [("succeeded", "")]


def test_core_owned_jobs_are_unaffected(scenario):
    with scenario.application_disabled(scenario.a):
        for job_type in ("test.core_tick", "test.subsystem_tick"):
            enqueue_in(job_type, scenario.a)
        run_pending_jobs()
    for job_type in ("test.core_tick", "test.subsystem_tick"):
        assert scheduled(job_type, scenario.a) == [("succeeded", "")]
    assert CALLS == [scenario.a.pk, scenario.a.pk]
