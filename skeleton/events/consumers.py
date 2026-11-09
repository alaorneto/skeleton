"""Deliver each event once per consumer through its own job, so one failure never blocks the others."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from skeleton.jobs.registry import JobType, register_job_type

CONSUME_JOB = "events.consume"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """What a consumer receives: identifiers and the declared, non-sensitive fields.

    The consumer runs in ``system_context`` of the organization and must
    re-authorize whatever it reads (for example ``require_record_action``
    for the person it is about to notify).
    """

    event_id: UUID
    event_type: str
    version: int
    organization_id: UUID
    table_key: str
    record_sysid: UUID
    record_revision: int | None
    actor_id: UUID | None
    request_id: UUID | None
    occurred_at: datetime
    data: MappingProxyType


def envelope(event: Any) -> EventEnvelope:
    return EventEnvelope(
        event_id=event.pk,
        event_type=event.event_type,
        version=event.version,
        organization_id=event.organization_id,
        table_key=event.table_key,
        record_sysid=event.record_sysid,
        record_revision=event.record_revision,
        actor_id=event.actor_id,
        request_id=event.request_id,
        occurred_at=event.occurred_at,
        data=MappingProxyType(dict(event.data)),
    )


def enqueue_deliveries(event: Any) -> int:
    """One job per consumer, keyed ``event:<id>:<consumer>``; called in the publishing transaction."""
    from skeleton.jobs.services import enqueue

    from .registry import consumers_for

    count = 0
    for consumer in consumers_for(event.event_type):
        enqueue(
            CONSUME_JOB,
            organization_id=event.organization_id,
            payload={"event_id": str(event.pk), "consumer": consumer.code},
            idempotency_key=f"event:{event.pk}:{consumer.code}",
        )
        count += 1
    return count


def consume(*, event_id: str, consumer: str) -> None:
    """Job handler: runs in system_context of the event's organization (tenant RLS applies)."""
    from .models import DomainEvent
    from .registry import get_consumer

    event = DomainEvent.objects.filter(pk=event_id).first()
    if event is None:
        return  # Purged by retention before delivery: nothing left to react to.
    try:
        handler = get_consumer(consumer).handler
    except KeyError:
        return  # The consumer was removed from code; the delivery is obsolete.
    handler(envelope(event))


def register_event_jobs() -> None:
    from .retention import purge_expired_events

    register_job_type(
        JobType(CONSUME_JOB, consume, {"event_id": "uuid", "consumer": "str"}, run_as="system", owner_app="sk_events")
    )
    register_job_type(
        JobType("events.retention", purge_expired_events, {}, run_as="system", max_attempts=3, owner_app="sk_events")
    )
