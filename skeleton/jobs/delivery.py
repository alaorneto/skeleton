"""Shared pieces of the delivery pipelines (webhooks, email, push): retry schedule and attempt jobs (#346).

Each pipeline keeps its own states, schedule values and idempotency key format (they are
persisted and documented); only the mechanics live here, so a fix is made once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class RetrySchedule:
    """Delay before attempt ``n + 1``, indexed by the attempts already made (1-based)."""

    delays: tuple[timedelta, ...]

    def delay_after(self, attempts: int) -> timedelta | None:
        """``None`` once the schedule is exhausted (or for a non-positive count)."""
        return self.delays[attempts - 1] if 1 <= attempts <= len(self.delays) else None

    @property
    def max_attempts(self) -> int:
        return len(self.delays) + 1


def enqueue_delivery_attempt(
    job_code: str,
    delivery: Any,
    *,
    idempotency_key: str,
    actor: Any = None,
    run_after: datetime | None = None,
) -> None:
    """One job for one attempt of ``delivery``; the caller owns the key format (it is persisted)."""
    from .services import enqueue

    enqueue(
        job_code,
        organization_id=delivery.organization_id,
        actor=actor,
        payload={"delivery_id": str(delivery.pk)},
        idempotency_key=idempotency_key,
        run_after=run_after,
    )
