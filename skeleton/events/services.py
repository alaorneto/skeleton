"""Publish a domain event in the caller's transaction: a rollback leaves no event."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError

from skeleton.common.exceptions import MissingTenantContext
from skeleton.tenancy.context import current_context

from .registry import get_event_type


def _json_value(name: str, value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise ValidationError({"data": f"{name}: use valores escalares (texto, número, booleano, UUID ou data)."})


def publish_event(code: str, *, record: Any, data: dict[str, Any] | None = None):
    """Store one event about ``record`` in the current tenant transaction.

    ``data`` keys must be the event type's declared ``payload_fields`` (read and
    non-sensitive fields of the table). The event carries the record revision and
    request ID for correlation with ``AuditEvent``, which remains the source of truth.
    """
    from .models import DomainEvent

    context = current_context()
    if context.organization_id is None:
        raise MissingTenantContext("Domain events are published inside a tenant or system tenant context.")
    try:
        event_type = get_event_type(code)
    except KeyError:
        raise ValidationError({"event_type": "Tipo de evento desconhecido."}) from None
    if record._meta.label_lower != event_type.table_key:
        raise ValidationError({"record": "O registro não pertence à tabela do tipo de evento."})
    if getattr(record, "organization_id", None) != context.organization_id:
        raise ValidationError({"record": "O registro pertence a outra organização."})
    data = dict(data or {})
    undeclared = set(data) - set(event_type.payload_fields)
    if undeclared:
        raise ValidationError({"data": f"Campos não declarados: {sorted(undeclared)}."})
    event = DomainEvent(
        organization_id=context.organization_id,
        event_type=event_type.code,
        version=event_type.version,
        table_key=event_type.table_key,
        record_sysid=record.pk,
        record_revision=getattr(record, "revision", None),
        actor_id=context.actor,
        request_id=context.request_id,
        source=context.source[:200],
        data={name: _json_value(name, value) for name, value in data.items()},
    )
    event.save(using=context.using)
    return event
