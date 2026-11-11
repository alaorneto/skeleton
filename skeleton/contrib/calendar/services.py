"""Calendar services (#68): every write of the app goes through here."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from skeleton.access.policies import has_table_permission
from skeleton.tenancy.context import current_context

from .models import Calendar

MEMBER_ACTIONS = ("read", "create", "update", "delete")


def _profile_timezone(user: Any) -> str:
    from skeleton.service_api.identity import IdentityService

    return IdentityService().me(actor=user).timezone or "UTC"


def ensure_personal_calendar(user: Any) -> Calendar:
    """The member's default personal calendar in the current organization, created once.

    Concurrent first visits race on the partial unique constraint; the loser reads the winner.
    Without table read permission the calendar does not exist for the member (404 upstream).
    """
    if not has_table_permission(Calendar, user=user, action="read"):
        raise PermissionDenied("Calendar table permission required.")
    existing = Calendar.objects.filter(owner_id=user.pk, kind=Calendar.Kind.PERSONAL, is_default=True).first()
    if existing is not None:
        return existing
    calendar = Calendar(
        organization_id=current_context().organization_id,
        owner_id=user.pk,
        visibility=Calendar.Visibility.PRIVATE,
        kind=Calendar.Kind.PERSONAL,
        is_default=True,
        name="Pessoal",
        timezone=_profile_timezone(user),
    )
    try:
        with transaction.atomic(using=current_context().using):
            calendar.save()
    except (IntegrityError, ValidationError):
        # Lost the race: the partial unique constraint (in full_clean or in the database) kept one.
        winner = Calendar.objects.filter(owner_id=user.pk, kind=Calendar.Kind.PERSONAL, is_default=True).first()
        if winner is None:
            raise
        return winner
    return calendar


# --------------------------------------------------------------------------- generic gateway operations


def create_calendar_record(record: Calendar, *, user: Any, values: dict) -> None:
    """Calendars created through the table gateway are shared calendars of the organization."""
    record.kind = Calendar.Kind.SHARED
    record.is_default = False
    record.save()


def update_calendar_record(record: Calendar, *, user: Any, values: dict) -> None:
    for name, value in values.items():
        setattr(record, name, value)
    record.save(update_fields=set(values))


def delete_calendar_record(record: Calendar, *, user: Any) -> None:
    if record.kind == Calendar.Kind.PERSONAL and record.is_default:
        raise ValidationError({"kind": "O calendário pessoal padrão não pode ser excluído."})
    record.delete()


def restore_calendar_record(record: Calendar, *, user: Any) -> None:
    record.restore()


# --------------------------------------------------------------------------- administration


def grant_member_access(*, actor: Any) -> tuple[str, ...]:
    """Organization admin action: basic calendar permissions for the built-in ``member`` role."""
    from skeleton.service_api.access import AccessService

    return AccessService().grant_member_table_access(
        actor=actor, table_key="skeleton_calendar.calendar", actions=MEMBER_ACTIONS
    )


# --------------------------------------------------------------------------- events (#69)

EVENT_FIELDS = (
    "title",
    "description",
    "location",
    "all_day",
    "starts_at",
    "ends_at",
    "start_date",
    "end_date",
    "tzid",
    "is_private",
    "status",
)


def _event_values(data: dict) -> dict:
    unknown = set(data) - set(EVENT_FIELDS)
    if unknown:
        raise ValidationError({"__all__": f"Campos não aceitos: {sorted(unknown)}."})
    values = dict(data)
    if values.get("all_day"):
        values["starts_at"] = values["ends_at"] = None
    elif "all_day" in values:
        values["start_date"] = values["end_date"] = None
    return values


def create_event(*, actor: Any, calendar_id: Any, data: dict) -> Any:
    """Create an event in a calendar the actor may update; the actor is the organizer."""
    from .models import Event
    from .policies import require_writable_calendar

    calendar = require_writable_calendar(actor, calendar_id)
    values = _event_values(data)
    values.setdefault("tzid", calendar.timezone)
    event = Event(organization_id=current_context().organization_id, calendar=calendar, organizer_id=actor.pk, **values)
    event.save()
    return event


def update_event(*, actor: Any, event_id: Any, data: dict, expected_revision: int) -> Any:
    """Change an event with CAS; every change bumps the iCalendar ``sequence``."""
    from skeleton.common.exceptions import ConcurrentUpdate

    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    if type(expected_revision) is not int or event.revision != expected_revision:
        raise ConcurrentUpdate("The event changed.")
    values = _event_values(data)
    for name, value in values.items():
        setattr(event, name, value)
    event.sequence += 1
    event.save(update_fields={*values, "sequence"})
    return event


def cancel_event(*, actor: Any, event_id: Any, expected_revision: int) -> Any:
    from .models import Event

    return update_event(
        actor=actor, event_id=event_id, data={"status": Event.Status.CANCELLED}, expected_revision=expected_revision
    )


def delete_event(*, actor: Any, event_id: Any, expected_revision: int) -> None:
    from skeleton.common.exceptions import ConcurrentUpdate

    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    if type(expected_revision) is not int or event.revision != expected_revision:
        raise ConcurrentUpdate("The event changed.")
    event.delete()
