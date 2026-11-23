"""Jobs and consumers of the insights (#710).

- ``insights.refresh`` (periodic, hourly, one job per organization): the snapshot.
- ``insights.refresh_organization`` (one organization, after a relevant event, deduplicated
  in 5-minute windows).
- ``insights.metrics`` (global, hourly): today's metrics; ``insights.daily`` (global, 00:20 UTC):
  the previous day closed and the retention of the metrics.

The schedules and the consumers exist only with ``SKELETON_INSIGHTS_ENABLED`` (read at start); the
job types are always registered, so a job already queued still runs.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

REFRESH_JOB = "insights.refresh_organization"
METRICS_JOB = "insights.metrics"
DAILY_JOB = "insights.daily"
CONSUMER = "insights.refresh"
EVENTS = (
    "membership.created",
    "membership.deactivated",
    "app.enabled",
    "app.disabled",
    "billing.invoice_paid",
    "billing.invoice_finalized",
    "billing.subscription_changed",
    "billing.access_state_changed",
)
WINDOW = timedelta(minutes=5)


def enqueue_refresh(organization_id: Any) -> None:
    """Ask for the snapshot of one organization, at most once per 5-minute window."""
    from django.utils import timezone

    from skeleton.jobs.services import enqueue

    now = timezone.now()
    bucket = int(now.timestamp() // WINDOW.total_seconds())
    enqueue(
        REFRESH_JOB,
        organization_id=organization_id,
        payload={},
        idempotency_key=f"insights:{organization_id}:{bucket}",
    )


def on_event(envelope: Any) -> None:
    """Consumer of the relevant events (system context of the event's organization)."""
    from skeleton.tenancy.context import current_context

    organization_id = current_context().organization_id
    if organization_id is not None:
        enqueue_refresh(organization_id)


def register_insights() -> None:
    from skeleton.events.registry import register_consumer
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    from .collect import daily, hourly_metrics, refresh_organization

    register_job_type(JobType(REFRESH_JOB, refresh_organization, {}, run_as="system", owner_app="sk_insights"))
    register_job_type(
        JobType(
            METRICS_JOB, hourly_metrics, {}, run_as="system", max_attempts=2, scope="global", owner_app="sk_insights"
        )
    )
    register_job_type(
        JobType(DAILY_JOB, daily, {}, run_as="system", max_attempts=3, scope="global", owner_app="sk_insights")
    )
    from django.conf import settings

    if not getattr(settings, "SKELETON_INSIGHTS_ENABLED", False):
        # Opt-in (#710): an instance that does not report to a central schedules nothing.
        return
    register_periodic("insights.refresh", REFRESH_JOB, every=timedelta(hours=1))
    register_periodic(METRICS_JOB, METRICS_JOB, every=timedelta(hours=1), per_organization=False)
    register_periodic(DAILY_JOB, DAILY_JOB, daily_at=time(0, 20), per_organization=False)
    for code in EVENTS:
        register_consumer(code, f"{CONSUMER}.{code}", on_event)
