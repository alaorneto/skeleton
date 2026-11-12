"""Domain events of the flow lifecycle (docs/flows.md, "Integrações").

The core types are about ``sk_flows.flowinstance`` and carry identifiers and the state only;
they are not offered to webhooks, because that table has no record-level authorization an
endpoint principal could satisfy. A flow may also map a moment to an event type **of its own
app about the subject record**: that one is an ordinary app event and reaches webhooks.
"""

from __future__ import annotations

from typing import Any

INSTANCE_TABLE = "sk_flows.flowinstance"
PAYLOAD = ("flow_key", "flow_version", "status", "outcome", "subject_table", "subject_sysid")
INSTANCE_EVENTS = {
    "started": "flows.instance_started",
    "completed": "flows.instance_completed",
    "cancelled": "flows.instance_cancelled",
    "failed": "flows.instance_failed",
}


def register_flow_event_types() -> None:
    from skeleton.events.registry import EventType, register_event_type

    for code in INSTANCE_EVENTS.values():
        register_event_type(EventType(code, table_key=INSTANCE_TABLE, payload_fields=PAYLOAD, owner_app="sk_flows"))


def publish_moment(execution: Any, moment: str) -> None:
    """Publish the core event of ``moment`` and the app's subject event, in the command's transaction."""
    from skeleton.events.services import publish_event
    from skeleton.system.registry import get_spec

    instance = execution.instance
    publish_event(
        INSTANCE_EVENTS[moment],
        record=instance,
        data={name: getattr(instance, name) for name in PAYLOAD},
    )
    code = execution.spec.subject_event(moment)
    if not code:
        return
    try:
        model = get_spec(instance.subject_table).model
    except KeyError:
        return  # The app is gone; there is no subject to talk about.
    subject = model.objects.filter(pk=instance.subject_sysid).first()
    if subject is not None:
        publish_event(code, record=subject, data={})
