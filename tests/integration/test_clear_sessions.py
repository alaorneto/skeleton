"""Expired sessions leave the table in a daily job (#677), in PostgreSQL as the runtime role."""

from datetime import timedelta

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone

from skeleton.accounts.sessions import CLEAR_SESSIONS_JOB, clear_expired_sessions
from skeleton.jobs.models import Job
from skeleton.jobs.periodic import schedule_due
from skeleton.jobs.services import enqueue
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs

pytestmark = pytest.mark.usefixtures("skeleton_db")


def session(*, expired: bool) -> str:
    store = SessionStore()
    store["k"] = "v"
    store.create()
    if expired:
        Session.objects.filter(pk=store.session_key).update(expire_date=timezone.now() - timedelta(days=1))
    return store.session_key


def test_the_job_deletes_only_expired_sessions():
    old = [session(expired=True) for _ in range(3)]
    live = session(expired=False)
    with system_context(reason="test:clear-sessions"):
        enqueue(CLEAR_SESSIONS_JOB, idempotency_key="test-clear-sessions")
    outcomes = run_pending_jobs()
    assert [outcome.state for outcome in outcomes if outcome.job_type == CLEAR_SESSIONS_JOB] == ["succeeded"]
    remaining = set(Session.objects.values_list("pk", flat=True))
    assert live in remaining and not set(old) & remaining


def test_one_run_stops_at_its_limit_and_the_next_goes_on():
    for _ in range(5):
        session(expired=True)
    assert clear_expired_sessions(per_run=3, batch=2) == 3
    assert Session.objects.filter(expire_date__lt=timezone.now()).count() == 2
    assert clear_expired_sessions(per_run=3, batch=2) == 2


def test_two_schedulers_create_one_job_per_window():
    seen_a, seen_b = {}, {}
    schedule_due(seen=seen_a)
    schedule_due(seen=seen_b)
    assert Job.all_objects.filter(job_type=CLEAR_SESSIONS_JOB).count() == 1
