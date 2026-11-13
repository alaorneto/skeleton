"""Claim, execute and retry jobs with one context per job.

Effects that stay in the database run exactly once: success is marked in the
handler's own transaction. External effects (SMTP, HTTP, push) are at least
once and need delivery records and idempotency keys of their own.
"""

from __future__ import annotations

import logging
import os
import random
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import connections, transaction
from django.db.models import F, Q
from django.utils import timezone

from skeleton.common.http_logging import bound_request_id, current_request_id
from skeleton.common.logging import security_event
from skeleton.observability.tracing import job_span, record_job_outcome

from .models import Job
from .registry import get_job_type

LEASE_MARGIN_S = 30
# A busy or idle worker writes its heartbeat at most this often; a state change writes at once (#243).
HEARTBEAT_EVERY_S = 15.0


@dataclass(frozen=True, slots=True)
class Outcome:
    job_id: UUID
    job_type: str
    state: str
    error_class: str = ""


def _log(
    event: str, job: Job, *, level: int = logging.INFO, exc_info: BaseException | None = None, **fields: Any
) -> None:
    # ``exc_info`` becomes a sanitized stack (file:line:function), never the message or the payload.
    security_event(
        event,
        level=level,
        source=f"job:{job.job_type}",
        target_sysid=str(job.pk),
        request_id=current_request_id(),
        exc_info=exc_info,
        **fields,
    )


def maintenance_paused() -> bool:
    marker = getattr(settings, "SKELETON_MAINTENANCE_MARKER", None)
    return bool(marker and Path(marker).exists())


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def heartbeat(worker_id: str, *, queues: tuple[str, ...], state: str = "running", using: str = "default") -> None:
    from .models import WorkerHeartbeat

    with transaction.atomic(using=using):
        row = WorkerHeartbeat.all_objects.using(using).select_for_update().filter(worker_id=worker_id).first()
        now = timezone.now()
        if row is None:
            WorkerHeartbeat(worker_id=worker_id, queues=",".join(queues), state=state, last_seen=now).save(using=using)
        else:
            row.queues, row.state, row.last_seen = ",".join(queues), state, now
            row.save(using=using, update_fields={"queues", "state", "last_seen"})


def reap_expired_leases(*, using: str = "default") -> int:
    """Requeue jobs whose lease expired (their worker vanished or overran); out of attempts, they become dead."""
    now = timezone.now()
    expired = Q(state=Job.State.RUNNING, locked_until__lt=now)
    with transaction.atomic(using=using):
        dead = (
            Job.all_objects.using(using)
            .filter(expired, attempts__gte=F("max_attempts"))
            .update(
                state=Job.State.DEAD,
                locked_by="",
                locked_until=None,
                finished_at=now,
                last_error_code="lease_expired",
            )
        )
        requeued = (
            Job.all_objects.using(using)
            .filter(expired)
            .update(state=Job.State.QUEUED, locked_by="", locked_until=None, last_error_code="lease_expired")
        )
    return dead + requeued


def claim(worker_id: str, *, queues: tuple[str, ...] = ("default",), using: str = "default") -> Job | None:
    """Take one due job with ``FOR UPDATE SKIP LOCKED``: concurrent workers never take the same job."""
    now = timezone.now()
    due = Job.all_objects.using(using).filter(state=Job.State.QUEUED, run_after__lte=now, queue__in=queues)
    with transaction.atomic(using=using):
        job = due.order_by("run_after", "created_at").select_for_update(skip_locked=True).first()
        if job is None:
            return None
        try:
            timeout = get_job_type(job.job_type).timeout_s
        except KeyError:
            timeout = 300
        locked_until = now + timedelta(seconds=timeout + LEASE_MARGIN_S)
        changed = (
            Job.all_objects.using(using)
            .filter(pk=job.pk, state=Job.State.QUEUED)
            .update(
                state=Job.State.RUNNING,
                attempts=F("attempts") + 1,
                locked_by=worker_id,
                locked_until=locked_until,
            )
        )
        if changed != 1:
            return None
    job.refresh_from_db(using=using)
    _log("job.claimed", job, count=job.attempts)
    return job


def _finish(job: Job, worker_id: str, state: str, *, using: str, **fields: Any) -> bool:
    return (
        Job.all_objects.using(using)
        .filter(pk=job.pk, state=Job.State.RUNNING, locked_by=worker_id)
        .update(state=state, locked_by="", locked_until=None, finished_at=timezone.now(), **fields)
        == 1
    )


def _retry_delay(job_type, attempts: int) -> timedelta:
    base = job_type.backoff * (2 ** max(attempts - 1, 0))
    return timedelta(seconds=min(base, 86400) * random.uniform(0.5, 1.5))  # noqa: S311 - jitter, not security


class JobTimeout(Exception):
    """The handler ran past its ``timeout_s``; the job follows the normal failure path (#297)."""

    code = "timeout"


@contextmanager
def _deadline(seconds: int) -> Iterator[None]:
    """Interrupt the handler after ``seconds`` (main thread only: signals cannot reach others)."""
    import signal
    import threading

    usable = hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    if not usable or seconds <= 0:
        yield
        return

    def expire(signum: int, frame: Any) -> None:
        raise JobTimeout()

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)  # Disarm before restoring the previous handler.
        signal.signal(signal.SIGALRM, previous)


def _set_lock_timeout(using: str, seconds: int) -> None:
    """Lock waits of the job's transaction end with its deadline, not after it (#297)."""
    from skeleton.common.db import bound_lock_waits

    if connections[using].vendor == "postgresql":
        bound_lock_waits(using=using, milliseconds=max(1, min(int(seconds) * 1000, 600_000)))


def _run_handler(job: Job, job_type, worker_id: str, using: str) -> None:
    from skeleton.tenancy.context import system_context, tenant_context

    payload = dict(job.payload)
    if job_type.run_as == "actor":
        from skeleton.accounts.models import User

        actor = User.all_objects.using(using).filter(pk=job.actor_id).first()
        if actor is None:
            raise PermissionDenied("Job actor is unavailable.")
        # A top-level tenant context revalidates the actor's membership: revoked access fails closed.
        with tenant_context(
            organization_id=job.job_organization_id, actor=actor, source=f"job:{job.job_type}", using=using
        ):
            _set_lock_timeout(using, job_type.timeout_s)
            with _deadline(job_type.timeout_s):
                job_type.handler(actor=actor, **payload)
            if not _finish(job, worker_id, Job.State.SUCCEEDED, using=using):
                raise RuntimeError("Job lease was lost.")
        return
    with system_context(reason=f"job:{job.job_type}", organization_id=job.job_organization_id, using=using):
        _set_lock_timeout(using, job_type.timeout_s)
        with _deadline(job_type.timeout_s):
            job_type.handler(**payload)
        if not _finish(job, worker_id, Job.State.SUCCEEDED, using=using):
            raise RuntimeError("Job lease was lost.")


def _owner_app_enabled(job: Job, job_type: Any, using: str) -> bool:
    """A job of an app disabled in its organization is cancelled before the handler (#337)."""
    if job.job_organization_id is None:
        return True
    from skeleton.tenancy.context import system_context

    from .registry import owner_app_enabled

    with system_context(reason=f"job-gate:{job.job_type}", organization_id=job.job_organization_id, using=using):
        return owner_app_enabled(job_type)


def _owner_app_suspended(job: Job, job_type: Any, using: str) -> bool:
    """A job of an app whose organization is suspended for billing is cancelled (#395)."""
    if job.job_organization_id is None:
        return False
    from skeleton.tenancy.context import system_context

    from .registry import owner_app_suspended

    with system_context(reason=f"job-gate:{job.job_type}", organization_id=job.job_organization_id, using=using):
        return owner_app_suspended(job_type)


def _organization_active(job: Job, using: str) -> bool:
    if job.job_organization_id is None:
        return True
    from skeleton.tenancy.models import Organization

    return (
        Organization.all_objects.using(using)
        .filter(pk=job.job_organization_id, is_active=True, deleted_at__isnull=True)
        .exists()
    )


def execute(job: Job, worker_id: str, *, using: str = "default") -> Outcome:
    """Run a claimed job; the handler's writes and the success mark commit together.

    Each execution gets its own ``request_id``: it correlates the job's log lines with its
    trace span and the SQL spans under it (#176).
    """
    with bound_request_id(uuid4()), job_span(job) as span:
        outcome = _execute(job, worker_id, using=using)
        record_job_outcome(span, outcome.state, outcome.error_class)
        return outcome


def _execute(job: Job, worker_id: str, *, using: str) -> Outcome:
    try:
        job_type = get_job_type(job.job_type)
    except KeyError:
        _finish(job, worker_id, Job.State.DEAD, using=using, last_error_code="unknown_job_type")
        _log("job.dead", job, level=logging.ERROR, reason_code="unknown_job_type")
        return Outcome(job.pk, job.job_type, Job.State.DEAD)
    if not _organization_active(job, using):
        _finish(job, worker_id, Job.State.CANCELLED, using=using, last_error_code="organization_inactive")
        _log("job.skipped", job, reason_code="organization_inactive")
        return Outcome(job.pk, job.job_type, Job.State.CANCELLED)
    if _owner_app_suspended(job, job_type, using):
        _finish(job, worker_id, Job.State.CANCELLED, using=using, last_error_code="organization_suspended")
        _log("job.skipped", job, reason_code="organization_suspended")
        return Outcome(job.pk, job.job_type, Job.State.CANCELLED)
    if not _owner_app_enabled(job, job_type, using):
        _finish(job, worker_id, Job.State.CANCELLED, using=using, last_error_code="application_disabled")
        _log("job.skipped", job, reason_code="application_disabled")
        return Outcome(job.pk, job.job_type, Job.State.CANCELLED)
    try:
        _run_handler(job, job_type, worker_id, using)
    except PermissionDenied:
        # Revoked actor access fails closed and is not retried: nothing was written.
        _finish(
            job,
            worker_id,
            Job.State.DEAD,
            using=using,
            last_error_class="PermissionDenied",
            last_error_code="actor_unauthorized",
        )
        _log("job.dead", job, level=logging.WARNING, reason_code="actor_unauthorized")
        return Outcome(job.pk, job.job_type, Job.State.DEAD, "PermissionDenied")
    except Exception as error:  # noqa: BLE001 - every failure is recorded, none escapes the worker
        if isinstance(error, JobTimeout):
            # A query cut in the middle may leave the connection unusable: record on a new one.
            connections[using].close()
        # The handler transaction is already rolled back; record the failure in a new one.
        error_class = type(error).__name__[:200]
        code = str(getattr(error, "code", "") or "")[:100]
        if job.attempts >= job_type.max_attempts:
            _finish(job, worker_id, Job.State.DEAD, using=using, last_error_class=error_class, last_error_code=code)
            _log("job.dead", job, level=logging.ERROR, exc_info=error, error_class=error_class, count=job.attempts)
            return Outcome(job.pk, job.job_type, Job.State.DEAD, error_class)
        Job.all_objects.using(using).filter(pk=job.pk, state=Job.State.RUNNING, locked_by=worker_id).update(
            state=Job.State.QUEUED,
            locked_by="",
            locked_until=None,
            run_after=timezone.now() + _retry_delay(job_type, job.attempts),
            last_error_class=error_class,
            last_error_code=code,
        )
        _log("job.failed", job, level=logging.WARNING, exc_info=error, error_class=error_class, count=job.attempts)
        return Outcome(job.pk, job.job_type, Job.State.QUEUED, error_class)
    _log("job.succeeded", job, outcome="succeeded")
    return Outcome(job.pk, job.job_type, Job.State.SUCCEEDED)


class Worker:
    """The loop behind ``run_worker``; ``stop()`` (SIGTERM) ends it after the current job."""

    def __init__(self, *, worker_id: str | None = None, queues: tuple[str, ...] = ("default",), using: str = "default"):
        self.worker_id = worker_id or default_worker_id()
        self.queues = tuple(queues)
        self.using = using
        self.stopping = False
        self._last_beat: float | None = None

    def stop(self, *args: Any) -> None:
        self.stopping = True

    def run_once(self) -> Outcome | None:
        if self.stopping or maintenance_paused():
            return None
        reap_expired_leases(using=self.using)
        job = claim(self.worker_id, queues=self.queues, using=self.using)
        if job is None:
            return None
        return execute(job, self.worker_id, using=self.using)

    def _beat(self, state: str = "running", *, force: bool = False) -> None:
        """Write the heartbeat at most every ``HEARTBEAT_EVERY_S``, or at once when forced."""
        now = time.monotonic()
        if not force and self._last_beat is not None and now - self._last_beat < HEARTBEAT_EVERY_S:
            return
        heartbeat(self.worker_id, queues=self.queues, state=state, using=self.using)
        self._last_beat = now

    def run(self, *, once: bool = False, max_jobs: int | None = None, idle_sleep: float = 1.0) -> list[Outcome]:
        # Every turn of the loop may beat, busy or idle: a worker that runs jobs back to back
        # used to write its heartbeat only when the queue emptied (#243).
        outcomes: list[Outcome] = []
        self._beat(force=True)
        try:
            while not self.stopping and (max_jobs is None or len(outcomes) < max_jobs):
                if maintenance_paused():
                    self._beat("paused", force=True)
                    if once:
                        break
                    time.sleep(idle_sleep)
                    continue
                self._beat()
                outcome = self.run_once()
                if outcome is None:
                    if once:
                        break
                    time.sleep(idle_sleep)
                    continue
                outcomes.append(outcome)
        finally:
            self._beat("stopped", force=True)
        return outcomes
