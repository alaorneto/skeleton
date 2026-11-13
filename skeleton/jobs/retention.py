"""Retention of finished jobs (#40): the only path that physically deletes ``Job`` rows.

The periodic global job ``jobs.retention`` runs every six hours in a system context. It
deletes only terminal jobs whose ``finished_at`` is older than their window:

- ``succeeded`` and ``cancelled`` after ``SKELETON_JOB_SUCCEEDED_RETENTION_DAYS`` (7);
- ``dead`` and ``failed`` after ``SKELETON_JOB_DEAD_RETENTION_DAYS`` (30), so operators
  have time to resend them from ``/platform/jobs/``.

Queued and running jobs are never touched, whatever their age. Heartbeats of workers that
stopped longer than the dead window are deleted too.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

RETENTION_JOB = "jobs.retention"
DEFAULT_SUCCEEDED_DAYS = 7
DEFAULT_DEAD_DAYS = 30
FINISHED_STATES = ("succeeded", "cancelled")
FAILED_STATES = ("dead", "failed")


def windows() -> dict[str, timedelta]:
    return {
        "succeeded": timedelta(
            days=int(getattr(settings, "SKELETON_JOB_SUCCEEDED_RETENTION_DAYS", DEFAULT_SUCCEEDED_DAYS))
        ),
        "dead": timedelta(days=int(getattr(settings, "SKELETON_JOB_DEAD_RETENTION_DAYS", DEFAULT_DEAD_DAYS))),
    }


def expired(now: datetime | None = None) -> dict[str, Any]:
    """The querysets the job deletes; exposed so tests can inspect the selection."""
    from .models import Job, WorkerHeartbeat

    now = now or timezone.now()
    limits = windows()
    jobs = Job.all_objects.filter(
        Q(state__in=FINISHED_STATES, finished_at__lt=now - limits["succeeded"])
        | Q(state__in=FAILED_STATES, finished_at__lt=now - limits["dead"])
    )
    heartbeats = WorkerHeartbeat.all_objects.filter(state="stopped", last_seen__lt=now - limits["dead"])
    return {"jobs": jobs, "heartbeats": heartbeats}


BATCH = 1000


def _delete_in_batches(queryset: Any) -> int:
    """Delete in blocks of ``BATCH`` primary keys: no single statement scans a huge backlog (#308)."""
    total = 0
    while True:
        ids = list(queryset.order_by("pk").values_list("pk", flat=True)[:BATCH])
        if not ids:
            return total
        count, _ = queryset.model.all_objects.filter(pk__in=ids).delete()
        total += count


def purge_expired_jobs() -> dict[str, int]:
    """Job handler (global, system context): delete what ``expired()`` selects."""
    selection = expired()
    jobs = _delete_in_batches(selection["jobs"])
    heartbeats = _delete_in_batches(selection["heartbeats"])
    return {"jobs": jobs, "heartbeats": heartbeats}


def register_retention_job() -> None:
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(code=RETENTION_JOB, handler=purge_expired_jobs, scope="global", owner_app="sk_jobs", max_attempts=3)
    )
    register_periodic(RETENTION_JOB, RETENTION_JOB, every=timedelta(hours=6), per_organization=False)
