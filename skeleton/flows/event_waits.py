"""The core consumer that advances event waits (#411).

It receives the domain events of the apps (core events never wake a wait) in the system
context of the event's organization, finds the running instances it may correlate (the event's
record, or a UUID among its declared fields) and advances their matching waits. Delivery is
one job per event, so a repeated delivery finds the wait already completed and does nothing.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

CONSUMER = "flows.event_waits"


def _accepts(event_type: Any) -> bool:
    return bool(event_type.owner_app) and not event_type.owner_app.startswith("sk_")


def _candidates(envelope: Any) -> set[UUID]:
    found = {envelope.record_sysid}
    for value in envelope.data.values():
        try:
            found.add(UUID(str(value)))
        except ValueError:
            continue
    return found


def deliver(envelope: Any) -> None:
    from .models import FlowInstance
    from .services import deliver_event

    instances = (
        FlowInstance.objects.filter(status="running", subject_sysid__in=_candidates(envelope))
        .order_by("sysid")
        .values_list("pk", flat=True)
    )
    for instance_id in list(instances):
        deliver_event(instance_id, envelope)


def register_event_waits() -> None:
    from skeleton.events.registry import WILDCARD, register_consumer

    register_consumer(WILDCARD, CONSUMER, deliver, accepts=_accepts)
