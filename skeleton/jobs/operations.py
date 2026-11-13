"""Operating the queue (#40): what ``/platform/jobs/`` and ``check_jobs`` read and change.

Nothing here reads or returns a payload: operators see the type, the organization, the
states and the error code only. Resending and discarding apply only to ``dead`` jobs and
are compare-and-swap on the state and ``updated_at`` the operator saw, so a double submit
or a concurrent change has exactly one winner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db.models import Count, Min
from django.utils import timezone

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.common.logging import security_event

from .models import Job, WorkerHeartbeat

LIST_FIELDS = (
    "sysid",
    "job_type",
    "queue",
    "job_organization_id",
    "state",
    "attempts",
    "max_attempts",
    "run_after",
    "last_error_class",
    "last_error_code",
    "finished_at",
    "created_at",
    "updated_at",
)


def jobs_for_operators(*, state: str = "", queue: str = "", job_type: str = ""):
    """Rows for the platform list, newest first, without the payload column."""
    queryset = Job.objects.only(*LIST_FIELDS).order_by("-created_at", "sysid")
    if state:
        queryset = queryset.filter(state=state)
    if queue:
        queryset = queryset.filter(queue=queue)
    if job_type:
        queryset = queryset.filter(job_type=job_type)
    return queryset


def state_counts() -> dict[str, int]:
    counts = dict.fromkeys(Job.State.values, 0)
    for row in Job.objects.values("state").annotate(total=Count("sysid")):
        counts[row["state"]] = row["total"]
    return counts


def job_type_state_counts() -> dict[tuple[str, str], int]:
    """Jobs per ``(job_type, state)``, every organization together: counts only, read at scrape time (#175)."""
    rows = Job.objects.values("job_type", "state").annotate(total=Count("sysid")).order_by()
    return {(row["job_type"], row["state"]): row["total"] for row in rows}


def _change_dead(job_id: UUID, *, expected_updated_at: datetime, actor: Any, event: str, **changes: Any) -> None:
    changed = Job.objects.filter(pk=job_id, state=Job.State.DEAD, updated_at=expected_updated_at).update(**changes)
    if changed != 1:
        raise ConcurrentUpdate("The job changed or is no longer dead.")
    security_event(event, source=f"platform:{actor.pk}", target_sysid=str(job_id), outcome="changed")


def resend_dead_job(job_id: UUID, *, expected_updated_at: datetime, actor: Any) -> None:
    """Put a dead job back in the queue with zero attempts, once."""
    _change_dead(
        job_id,
        expected_updated_at=expected_updated_at,
        actor=actor,
        event="job.resent",
        state=Job.State.QUEUED,
        attempts=0,
        run_after=timezone.now(),
        finished_at=None,
        locked_by="",
        locked_until=None,
        last_error_class="",
        last_error_code="",
    )


def discard_dead_job(job_id: UUID, *, expected_updated_at: datetime, actor: Any) -> None:
    """Mark a dead job cancelled; the retention deletes it later like any finished job."""
    _change_dead(
        job_id,
        expected_updated_at=expected_updated_at,
        actor=actor,
        event="job.discarded",
        state=Job.State.CANCELLED,
        finished_at=timezone.now(),
        last_error_code="discarded",
    )


@dataclass(frozen=True, slots=True)
class QueueHealth:
    due: int
    oldest_due_lag_s: float
    dead: int
    live_workers: int
    stale_workers: int

    def problems(self, *, max_lag_s: float, max_dead: int | None, require_worker: bool) -> list[str]:
        found = []
        if self.oldest_due_lag_s > max_lag_s:
            found.append(f"queue_lag {int(self.oldest_due_lag_s)}s > {int(max_lag_s)}s")
        if max_dead is not None and self.dead > max_dead:
            found.append(f"dead_jobs {self.dead} > {max_dead}")
        if require_worker and self.live_workers == 0:
            found.append("no_live_worker")
        return found


def queue_health(*, now: datetime | None = None, heartbeat_s: int = 120) -> QueueHealth:
    """Counts only: how late the oldest due job is, how many are dead, which workers answer."""
    now = now or timezone.now()
    due = Job.objects.filter(state=Job.State.QUEUED, run_after__lte=now).aggregate(
        total=Count("sysid"), oldest=Min("run_after")
    )
    lag = (now - due["oldest"]).total_seconds() if due["oldest"] else 0.0
    cutoff = now - timedelta(seconds=heartbeat_s)
    # A worker inside a long job has an old heartbeat but a valid lease: it is alive (#243).
    busy = set(Job.objects.filter(state=Job.State.RUNNING, locked_until__gt=now).values_list("locked_by", flat=True))
    rows = list(WorkerHeartbeat.objects.filter(state__in=("running", "paused")).values_list("worker_id", "last_seen"))
    live = sum(1 for worker_id, last_seen in rows if last_seen >= cutoff or worker_id in busy)
    return QueueHealth(
        due=due["total"],
        oldest_due_lag_s=max(lag, 0.0),
        dead=Job.objects.filter(state=Job.State.DEAD).count(),
        live_workers=live,
        stale_workers=len(rows) - live,
    )
