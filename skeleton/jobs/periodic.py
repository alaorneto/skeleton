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
        assert self.daily_at is not None  # noqa: S101 - type narrowing; the constructor requires every or daily_at
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


def _existing_keys(job_type: str, keys: list[str], using: str) -> set[str]:
    """Idempotency keys of ``job_type`` already in the queue, read in blocks of 1 000 (#240)."""
    from .models import Job

    found: set[str] = set()
    for start in range(0, len(keys), 1000):
        found.update(
            key
            for key in Job.all_objects.using(using)
            .filter(job_type=job_type, idempotency_key__in=keys[start : start + 1000])
            .values_list("idempotency_key", flat=True)
            if key is not None
        )
    return found


def _window_memory(seen: dict[str, tuple[str, set[Any]]] | None, code: str, stamp: str) -> set[Any]:
    """What this process already scheduled for ``code`` in the window ``stamp`` (empty on a new window)."""
    if seen is not None and code in seen and seen[code][0] == stamp:
        return seen[code][1]
    done: set[Any] = set()
    if seen is not None:
        seen[code] = (stamp, done)
    return done


def schedule_due(
    *, now: datetime | None = None, using: str = "default", seen: dict[str, tuple[str, set[Any]]] | None = None
) -> int:
    """Enqueue the current window of every periodic job; returns how many jobs were created.

    ``seen`` (kept by ``run_scheduler`` between ticks) remembers what this process already
    scheduled in the current window, and the keys already in the queue are read in one query
    per job type: a tick of a quiet scheduler inserts nothing and fails no unique constraint
    (#240). Without ``seen`` the query alone avoids the inserts. Two schedulers racing still
    end with one job per key: ``enqueue`` absorbs the conflict.
    """
    from skeleton.tenancy.context import system_context

    from .registry import get_job_type, owner_app_enabled, owner_app_suspended
    from .services import enqueue

    now = now or timezone.now()
    created = 0
    organizations = None
    for periodic in all_periodic():
        window = periodic.window_start(now)
        stamp = window.strftime("%Y%m%dT%H%M%SZ")
        done = _window_memory(seen, periodic.code, stamp)
        payload = dict(periodic.payload)
        job_type = get_job_type(periodic.job_type)
        if "window" in job_type.payload_schema:
            # The handler decides by the window it was scheduled for, not by when it runs (#276).
            payload["window"] = stamp
        if periodic.per_organization:
            if organizations is None:
                organizations = _active_organization_ids(using)
            pending = [organization_id for organization_id in organizations if organization_id not in done]
            keys = {
                f"periodic:{periodic.code}:{organization_id}:{stamp}": organization_id for organization_id in pending
            }
            existing = _existing_keys(periodic.job_type, list(keys), using) if keys else set()
            for key, organization_id in keys.items():
                if key not in existing:
                    # One independent transaction per organization.
                    with system_context(
                        reason=f"periodic:{periodic.code}", organization_id=organization_id, using=using
                    ):
                        if not owner_app_enabled(job_type) or owner_app_suspended(job_type):
                            # The app is off here (#337), or its organization is suspended (#395):
                            # not remembered, so reopening it schedules this same window on the next tick.
                            continue
                        receipt = enqueue(
                            periodic.job_type,
                            organization_id=organization_id,
                            payload=payload,
                            idempotency_key=key,
                            run_after=window,
                        )
                    created += receipt.created
                done.add(organization_id)
        elif None not in done:
            key = f"periodic:{periodic.code}:{stamp}"
            if not _existing_keys(periodic.job_type, [key], using):
                with system_context(reason=f"periodic:{periodic.code}", using=using):
                    receipt = enqueue(
                        periodic.job_type,
                        payload=payload,
                        idempotency_key=key,
                        run_after=window,
                    )
                created += receipt.created
            done.add(None)
    return created
