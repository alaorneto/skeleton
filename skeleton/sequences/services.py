"""Gapless numbers per organization and period, allocated in the caller's transaction (#155).

The counter row of ``(organization, key, period)`` is locked with ``SELECT … FOR UPDATE``;
its first creation is serialized on the organization lock, so two transactions never both
create it. That lock is ``FOR NO KEY UPDATE`` (#239): it does not conflict with the inserts the
transaction already made, whose foreign keys take ``FOR KEY SHARE`` on the organization. Incrementing in the caller's transaction is what makes the numbering gapless: a
rollback of the business write also rolls the counter back, and the next allocation reuses
the number. The price is that allocations of one sequence and period in one organization
run one at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import MissingTenantContext
from skeleton.tenancy.context import current_context

from .registry import get_sequence


@dataclass(frozen=True, slots=True)
class SequenceNumber:
    key: str
    period: str
    number: int
    formatted: str


def period_of(spec: Any, at: datetime) -> str:
    moment = at.astimezone(UTC)
    if spec.period == "year":
        return f"{moment.year:04d}"
    if spec.period == "month":
        return f"{moment.year:04d}-{moment.month:02d}"
    return ""


def next_number(key: str, *, at: datetime | None = None) -> SequenceNumber:
    """The next number of ``key`` for the current organization; tenant context required."""
    from skeleton.access.services import organization_lock

    from .models import SequenceCounter

    if current_context().organization_id is None:
        raise MissingTenantContext("Sequences are numbered per organization.")
    try:
        spec = get_sequence(key)
    except KeyError:
        raise ValidationError({"key": gettext("Sequência não declarada.")}) from None
    moment = at or timezone.now()
    if timezone.is_naive(moment):
        raise ValidationError({"at": gettext("Use data e hora com fuso.")})
    period = period_of(spec, moment)
    with transaction.atomic():
        counter = SequenceCounter.objects.select_for_update().filter(key=key, period=period).first()
        if counter is None:
            with organization_lock() as organization:
                counter = SequenceCounter.objects.select_for_update().filter(key=key, period=period).first()
                if counter is None:
                    counter = SequenceCounter(organization=organization, key=key, period=period, last_value=0)
                    counter.save()
        counter.last_value += 1
        counter.save(update_fields={"last_value"})
    utc = moment.astimezone(UTC)
    formatted = spec.template.format(
        number=counter.last_value, year=f"{utc.year:04d}", month=f"{utc.month:02d}", period=period
    )
    return SequenceNumber(key=key, period=period, number=counter.last_value, formatted=formatted)
