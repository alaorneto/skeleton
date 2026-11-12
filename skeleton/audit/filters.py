"""Filters of the audit log shared by the organization page, the platform page and the API (#184).

The query string uses ``from``, ``to``, ``actor``, ``operation``, ``request_id``,
``model_label`` and ``record_sysid``; anything else, or a value that does not parse, is a 400.
``from`` and ``to`` accept a date (``2026-11-12``, the whole day in the person's time zone) or a
date and time in ISO 8601. Filtering never widens what ``authorized_audit_queryset`` allows: it
only narrows the events the person could already read.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.translation import gettext

QUERY_FILTERS = ("from", "to", "actor", "operation", "request_id", "model_label", "record_sysid")
PAGING = ("page", "page_size")


def _moment(name: str, value: str, *, end: bool) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        day = parse_date(value)
        if day is None:
            raise ValidationError({name: gettext("Use uma data (AAAA-MM-DD) ou data e hora ISO 8601.")})
        parsed = datetime.combine(day + timedelta(days=1) if end else day, time.min)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _uuid(name: str, value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ValidationError({name: gettext("Identificador inválido.")}) from None


def parse_audit_filters(params: Any) -> dict[str, Any]:
    """Keyword arguments for ``AuditLogService.events`` from a query string (blank values ignored)."""
    from .models import AuditEvent

    unknown = set(params) - set(QUERY_FILTERS) - set(PAGING)
    if unknown:
        raise ValidationError(gettext("Filtro não permitido."))
    values = {name: (params.get(name) or "").strip() for name in QUERY_FILTERS}
    result: dict[str, Any] = {}
    if values["from"]:
        result["since"] = _moment("from", values["from"], end=False)
    if values["to"]:
        result["until"] = _moment("to", values["to"], end=True)
    if values["actor"]:
        result["actor_id"] = _uuid("actor", values["actor"])
    if values["operation"]:
        if values["operation"] not in AuditEvent.Operation.values:
            raise ValidationError({"operation": gettext("Operação desconhecida.")})
        result["operation"] = values["operation"]
    if values["request_id"]:
        result["request_id"] = _uuid("request_id", values["request_id"])
    if values["model_label"]:
        result["model_label"] = values["model_label"]
    if values["record_sysid"]:
        result["record_id"] = _uuid("record_sysid", values["record_sysid"])
    return result


def operation_choices() -> list[tuple[str, str]]:
    """The operations a person can filter by, with their labels (for the filter forms)."""
    from .models import AuditEvent

    return list(AuditEvent.Operation.choices)
