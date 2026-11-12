"""Periodic jobs declared in code, scheduled idempotently per time window.

Several schedulers may run at once without leader election: each window and
organization maps to one idempotency key, so the database keeps exactly one
job. Only the current window is scheduled, so a scheduler that was down
recovers at most the window in progress, never a backlog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

from django.utils import timezone
from django.utils.translation import gettext

_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class PeriodicJob:
    code: str
    job_type: str
    every: timedelta | None = None
    daily_at: time | None = None
    per_organization: bool = True
    payload: dict[str, Any] = field(default_factory=dict)

    def window_start(self, now: datetime) -> datetime:
        now = now.astimezone(UTC)
        if self.every is not None:
            steps = (now - _EPOCH) // self.every
            return _EPOCH + steps * self.every
        assert self.daily_at is not None
        today = datetime.combine(now.date(), self.daily_at, tzinfo=UTC)
        return today if now >= today else today - timedelta(days=1)


_periodic: dict[str, PeriodicJob] = {}


def register_periodic(
    code: str,
    job_type: str,
    *,
    every: timedelta | None = None,
    daily_at: time | None = None,
    per_organization: bool = True,
    payload: dict[str, Any] | None = None,
) -> PeriodicJob:
    """Declare in ``AppConfig.ready()``: exactly one of ``every`` or ``daily_at`` (UTC)."""
    if (every is None) == (daily_at is None):
        raise ValueError("Use exactly one of every or daily_at.")
    if every is not None and every < timedelta(minutes=1):
        raise ValueError("Periodic jobs run at most once a minute.")
    periodic = PeriodicJob(code, job_type, every, daily_at, per_organization, dict(payload or {}))
    if code in _periodic and _periodic[code] != periodic:
        raise ValueError(f"Duplicate periodic job: {code}")
    _periodic[code] = periodic
    return periodic


def all_periodic() -> tuple[PeriodicJob, ...]:
    return tuple(_periodic[code] for code in sorted(_periodic))


def periodic_problems() -> list[str]:
    from .registry import get_job_type

    problems = []
    for periodic in all_periodic():
        try:
            job_type = get_job_type(periodic.job_type)
        except KeyError:
            problems.append(f"{periodic.code}: unknown job type {periodic.job_type}")
            continue
        if periodic.per_organization != (job_type.scope == "organization"):
            problems.append(f"{periodic.code}: per_organization must match the job type scope")
        if job_type.run_as != "system":
            problems.append(gettext("%(code)s: periodic jobs run as system") % {"code": periodic.code})
    return problems


def _active_organization_ids(using: str) -> list:
    from skeleton.tenancy.models import Organization

    # Materialized before enqueuing: no global transaction wraps the per-organization loop.
    return list(
        Organization.all_objects.using(using)
        .filter(is_active=True, deleted_at__isnull=True)
        .order_by("sysid")
        .values_list("sysid", flat=True)
    )


def schedule_due(*, now: datetime | None = None, using: str = "default") -> int:
    """Enqueue the current window of every periodic job; returns how many jobs were created."""
    from skeleton.tenancy.context import system_context

    from .services import enqueue

    now = now or timezone.now()
    created = 0
    organizations = None
    for periodic in all_periodic():
        window = periodic.window_start(now)
        stamp = window.strftime("%Y%m%dT%H%M%SZ")
        if periodic.per_organization:
            if organizations is None:
                organizations = _active_organization_ids(using)
            for organization_id in organizations:
                # One independent transaction per organization.
                with system_context(reason=f"periodic:{periodic.code}", organization_id=organization_id, using=using):
                    receipt = enqueue(
                        periodic.job_type,
                        organization_id=organization_id,
                        payload=periodic.payload,
                        idempotency_key=f"periodic:{periodic.code}:{organization_id}:{stamp}",
                        run_after=window,
                    )
                created += receipt.created
        else:
            with system_context(reason=f"periodic:{periodic.code}", using=using):
                receipt = enqueue(
                    periodic.job_type,
                    payload=periodic.payload,
                    idempotency_key=f"periodic:{periodic.code}:{stamp}",
                    run_after=window,
                )
            created += receipt.created
    return created
