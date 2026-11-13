"""Absence substitution in flows (#418): a person's tasks go to a substitute for a period.

The person records their own absence; who administers flows records it for anyone. The
substitute must be another active human member. When a task is assigned to someone away, it
goes to the substitute (``delegated_from`` keeps the person and the history says so), provided
the substitute reads the business record; otherwise the task stays with the person and the
history records why. Only tasks created during the period move; queue tasks have no assignee.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

MAX_DAYS = 366


@dataclass(frozen=True)
class AbsenceInfo:
    sysid: UUID
    revision: int
    user_id: UUID
    substitute_id: UUID
    starts_at: datetime
    ends_at: datetime


def _info(row: Any) -> AbsenceInfo:
    return AbsenceInfo(row.pk, row.revision, row.user_id, row.substitute_id, row.starts_at, row.ends_at)


def _uuid(value: Any, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError):
        raise ValidationError({field: gettext("Identificador inválido.")}) from None


def _moment(value: Any, field: str) -> datetime:
    if isinstance(value, str):
        from django.utils.dateparse import parse_datetime

        value = parse_datetime(value)
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValidationError({field: gettext("Use data e hora com fuso (ISO 8601).")})
    return value


def _active_human_member(user_id: UUID) -> bool:
    from skeleton.tenancy.models import Membership

    return Membership.objects.filter(
        user_id=user_id, is_active=True, user__is_active=True, user__account_type="human"
    ).exists()


def absences(actor: Any) -> tuple[AbsenceInfo, ...]:
    """The live absences the actor sees: their own, or every one for who administers flows."""
    from . import policies
    from .models import FlowAbsence

    rows = FlowAbsence.objects.all()
    if not policies.is_flow_admin(actor):
        rows = rows.filter(user_id=actor.pk)
    return tuple(_info(row) for row in rows.order_by("starts_at", "sysid"))


def register_absence(actor: Any, *, user_id: Any, substitute_id: Any, starts_at: Any, ends_at: Any) -> AbsenceInfo:
    from skeleton.tenancy.context import current_context

    from . import policies
    from .models import FlowAbsence

    person, substitute = _uuid(user_id, "user"), _uuid(substitute_id, "substitute")
    start, end = _moment(starts_at, "starts_at"), _moment(ends_at, "ends_at")
    if person != actor.pk and not policies.is_flow_admin(actor):
        raise PermissionDenied("Only the person or who administers flows records an absence.")
    if person == substitute:
        raise ValidationError({"substitute": gettext("O substituto é outra pessoa.")})
    if end <= start or (end - start).days > MAX_DAYS:
        raise ValidationError({"ends_at": gettext("O fim vem depois do início, em até um ano.")})
    if not _active_human_member(person) or not _active_human_member(substitute):
        raise ValidationError({"substitute": gettext("Pessoa e substituto precisam ser membros ativos.")})
    row = FlowAbsence(
        organization_id=current_context().organization_id,
        user_id=person,
        substitute_id=substitute,
        starts_at=start,
        ends_at=end,
    )
    row.save()
    return _info(row)


def remove_absence(actor: Any, *, absence_id: Any, expected_revision: Any) -> None:
    from . import policies
    from .models import FlowAbsence

    with transaction.atomic():
        row = FlowAbsence.objects.select_for_update().filter(pk=_uuid(absence_id, "absence")).first()
        if row is None or (row.user_id != actor.pk and not policies.is_flow_admin(actor)):
            raise FlowAbsence.DoesNotExist("Absence unavailable.")
        if type(expected_revision) is not int or row.revision != expected_revision:
            raise ConcurrentUpdate("The absence changed.")
        row.delete()


def substitute_for(execution: Any, run: Any, user_id: Any) -> tuple[Any, Any]:
    """``(assignee, delegated_from)`` for a task about to be assigned to ``user_id``."""
    from skeleton.access.policies import users_who_can_read
    from skeleton.system.registry import get_spec

    from .models import FlowAbsence

    if user_id is None:
        return None, None
    away = (
        FlowAbsence.objects.filter(user_id=user_id, starts_at__lte=execution.now, ends_at__gt=execution.now)
        .order_by("starts_at", "sysid")
        .first()
    )
    if away is None:
        return user_id, None
    instance = execution.instance
    subject = get_spec(instance.subject_table).model.objects.filter(pk=instance.subject_sysid).first()
    readable = subject is not None and away.substitute_id in users_who_can_read(subject, [away.substitute_id])
    if not readable or not _active_human_member(away.substitute_id):
        execution.log("absence_substitute_refused", run=run, detail={"absence": str(away.pk)})
        return user_id, None
    execution.log("absence_delegated", run=run, detail={"absence": str(away.pk)})
    return away.substitute_id, user_id
