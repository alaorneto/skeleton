"""Expired sessions leave ``django_session`` (#677): a daily global job, in blocks.

Sessions live in the database (``SESSION_ENGINE = "...sessions.backends.db"``) and Django keeps the
expired rows until ``clearsessions`` runs. With many visitors the table grows without bound and
weighs on backups, vacuum and the agent's dump. The job deletes the rows whose ``expire_date``
passed, in blocks of ``BATCH`` keys, at most ``PER_RUN`` rows per run; the rest waits for the next
day. Several schedulers create one job per window (``skeleton.jobs.periodic``).
"""

from __future__ import annotations

from datetime import time

from django.utils import timezone

CLEAR_SESSIONS_JOB = "accounts.clear_sessions"
BATCH = 1000
PER_RUN = 100_000


def clear_expired_sessions(*, now=None, per_run: int = PER_RUN, batch: int = BATCH) -> int:
    """Delete expired sessions; returns how many rows left the table."""
    from django.contrib.sessions.models import Session

    now = now or timezone.now()
    expired = Session.objects.filter(expire_date__lt=now)
    total = 0
    while total < per_run:
        keys = list(expired.order_by("pk").values_list("pk", flat=True)[: min(batch, per_run - total)])
        if not keys:
            break
        count, _ = Session.objects.filter(pk__in=keys, expire_date__lt=now).delete()
        total += count
    return total


def run_clear_sessions() -> dict[str, int]:
    """Job handler (global, system context)."""
    return {"sessions": clear_expired_sessions()}


def register_session_jobs() -> None:
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(
            CLEAR_SESSIONS_JOB,
            run_clear_sessions,
            {},
            run_as="system",
            max_attempts=3,
            scope="global",
            owner_app="sk_accounts",
        )
    )
    register_periodic(CLEAR_SESSIONS_JOB, CLEAR_SESSIONS_JOB, daily_at=time(3, 30), per_organization=False)
