"""Audit log filters shared by the organization page, the platform page and ``/audit/`` (#184).

The surfaces parse the query string here and hand ``AuditLogService.events`` typed values.
The filters are an allowlist: an unknown parameter or an invalid value is a ``ValidationError``
(400), never a silent no-op, so a mistyped filter cannot look like "no events".
"""

from datetime import datetime, time, timedelta
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

#: Query parameter -> keyword of ``AuditLogService.events``.
FILTER_PARAMETERS = {
    "from": "occurred_from",
    "to": "occurred_to",
    "actor": "actor_id",
    "operation": "operation",
    "request_id": "request_id",
    "model_label": "model_label",
    "record_sysid": "record_id",
}
AUDIT_OPERATION_CHOICES = (
    ("create", "Criar"),
    ("update", "Atualizar"),
    ("soft_delete", "Excluir logicamente"),
    ("restore", "Restaurar"),
    ("hard_delete", "Excluir fisicamente"),
)
OPERATIONS = tuple(code for code, _ in AUDIT_OPERATION_CHOICES)
MAX_LABEL_LENGTH = 255


def _moment(value, *, end):
    """Both bounds are inclusive: a date covers the whole day in the active time zone."""
    # A bare date first: parse_datetime would read it as midnight.
    day = parse_date(value) if len(value) == 10 else None
    parsed = None if day is not None else parse_datetime(value)
    if parsed is None:
        if day is None:
            raise ValueError
        if end:
            parsed = datetime.combine(day + timedelta(days=1), time.min) - timedelta(microseconds=1)
        else:
            parsed = datetime.combine(day, time.min)
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def parse_audit_filters(params, *, allowed=("page", "page_size")):
    """Return the ``AuditLogService.events`` keywords of a QueryDict or mapping.

    Empty values are ignored (the HTML form sends every field); ``allowed`` lists the other
    parameters the surface accepts, such as pagination.
    """
    unknown = set(params) - set(FILTER_PARAMETERS) - set(allowed)
    if unknown:
        raise ValidationError("Filtro não permitido.")
    if hasattr(params, "getlist") and any(len(params.getlist(key)) != 1 for key in params):
        raise ValidationError("Filtro repetido.")
    result = {}
    for name, keyword in FILTER_PARAMETERS.items():
        value = params.get(name)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValidationError({name: "Filtro inválido."})
        try:
            if name in {"from", "to"}:
                result[keyword] = _moment(value, end=name == "to")
            elif name in {"actor", "request_id", "record_sysid"}:
                result[keyword] = UUID(value)
            elif name == "operation":
                if value not in OPERATIONS:
                    raise ValueError
                result[keyword] = value
            else:
                if len(value) > MAX_LABEL_LENGTH:
                    raise ValueError
                result[keyword] = value
        except (ValueError, TypeError, OverflowError):
            raise ValidationError({name: "Filtro inválido."}) from None
    start, end = result.get("occurred_from"), result.get("occurred_to")
    if start is not None and end is not None and start > end:
        raise ValidationError({"from": "O início é posterior ao fim."})
    return result


def filter_form_values(params):
    """Raw values to refill the HTML filter form (escaped by the template)."""
    return {name: params.get(name, "") for name in FILTER_PARAMETERS}
