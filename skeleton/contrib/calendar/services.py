"""Calendar services (#68): every write of the app goes through here."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext

from skeleton.access.policies import has_table_permission
from skeleton.tenancy.context import current_context

from .models import Calendar

MEMBER_ACTIONS = ("read", "create", "update", "delete")


def _profile_timezone(user: Any) -> str:
    from skeleton.service_api.identity import IdentityService

    return IdentityService().me(actor=user).effective_timezone or "UTC"


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
        raise ValidationError({"kind": gettext("O calendário pessoal padrão não pode ser excluído.")})
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
    "rrule",
    "rdates",
    "exdates",
)
COPIED_FIELDS = (*EVENT_FIELDS[:-3], "tzid")


def _event_values(data: dict) -> dict:
    unknown = set(data) - set(EVENT_FIELDS)
    if unknown:
        raise ValidationError({"__all__": gettext("Campos não aceitos: %(value1)s.") % {"value1": sorted(unknown)}})
    values = dict(data)
    if values.get("all_day"):
        values["starts_at"] = values["ends_at"] = None
    elif "all_day" in values:
        values["start_date"] = values["end_date"] = None
    return values


def create_event(*, actor: Any, calendar_id: Any, data: dict, uid: str | None = None) -> Any:
    """Create an event in a calendar the actor may update; the actor is the organizer."""
    from .models import Event
    from .policies import require_writable_calendar

    calendar = require_writable_calendar(actor, calendar_id)
    values = _event_values(data)
    values.setdefault("tzid", calendar.timezone)
    if uid is not None:
        values["uid"] = uid
    event = Event(organization_id=current_context().organization_id, calendar=calendar, organizer_id=actor.pk, **values)
    event.save()
    return event


def update_event(*, actor: Any, event_id: Any, data: dict, expected_revision: int) -> Any:
    """Change an event with CAS; every change bumps the iCalendar ``sequence``."""
    from skeleton.service_api import Conflict

    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    if type(expected_revision) is not int or event.revision != expected_revision:
        raise Conflict("stale_revision", gettext("O evento mudou."))
    values = _event_values(data)
    for name, value in values.items():
        setattr(event, name, value)
    event.sequence += 1
    event.save(update_fields={*values, "sequence", "recurrence_end"})
    from .attendees import CANCELLED, UPDATED, notify_attendees

    notify_attendees(event, CANCELLED if values.get("status") == "cancelled" else UPDATED, actor=actor)
    return event


def cancel_event(*, actor: Any, event_id: Any, expected_revision: int) -> Any:
    from .models import Event

    return update_event(
        actor=actor, event_id=event_id, data={"status": Event.Status.CANCELLED}, expected_revision=expected_revision
    )


def delete_event(*, actor: Any, event_id: Any, expected_revision: int) -> None:
    from skeleton.service_api import Conflict

    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    if type(expected_revision) is not int or event.revision != expected_revision:
        raise Conflict("stale_revision", gettext("O evento mudou."))
    event.delete()


# --------------------------------------------------------------------------- recurrence edits (#70)

EDIT_MODES = ("this", "following", "all")


def _master(actor: Any, event_id: Any, expected_revision: int) -> Any:
    from skeleton.service_api import Conflict

    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    if event.series_id is not None or not event.rrule:
        raise ValidationError({"event": gettext("Use o evento mestre de uma série recorrente.")})
    if type(expected_revision) is not int or event.revision != expected_revision:
        raise Conflict("stale_revision", gettext("A série mudou."))
    return event


def _occurrence(master: Any, recurrence_id: Any) -> Any:
    from datetime import timedelta

    from .recurrence import expand

    window = expand(master, recurrence_id - timedelta(days=1), recurrence_id + timedelta(days=1))
    if recurrence_id not in {occurrence.recurrence_id for occurrence in window.occurrences}:
        raise ValidationError({"recurrence_id": gettext("A série não tem ocorrência nesse instante.")})
    return recurrence_id


def _copy(master: Any, **changes: Any) -> Any:
    from .models import Event

    values = {name: getattr(master, name) for name in COPIED_FIELDS}
    values.update(changes)
    return Event(
        organization_id=master.organization_id,
        calendar_id=master.calendar_id,
        organizer_id=master.organizer_id,
        **values,
    )


def _shifted(master: Any, recurrence_id: Any) -> dict:
    """Start/end fields of the occurrence that starts at ``recurrence_id`` (same duration)."""
    if master.all_day:
        days = master.end_date - master.start_date
        return {"start_date": recurrence_id.date(), "end_date": recurrence_id.date() + days}
    return {"starts_at": recurrence_id, "ends_at": recurrence_id + (master.ends_at - master.starts_at)}


def edit_occurrence(
    *, actor: Any, event_id: Any, recurrence_id: Any, mode: str, data: dict, expected_revision: int
) -> Any:
    """Change one occurrence, this and the following ones, or the whole series."""
    from .models import Event

    if mode not in EDIT_MODES:
        raise ValidationError({"mode": "Use this, following ou all."})
    master = _master(actor, event_id, expected_revision)
    if mode == "all":
        return update_event(actor=actor, event_id=master.pk, data=data, expected_revision=expected_revision)
    _occurrence(master, recurrence_id)
    values = _event_values({key: value for key, value in data.items() if key not in ("rrule", "rdates", "exdates")})
    if mode == "this":
        override = Event.objects.filter(series=master, recurrence_id=recurrence_id).first()
        if override is None:
            override = _copy(master, **{**_shifted(master, recurrence_id), **values})
            override.uid, override.series, override.recurrence_id = master.uid, master, recurrence_id
            override.rrule, override.rdates, override.exdates = "", [], []
            override.save()
        else:
            for name, value in values.items():
                setattr(override, name, value)
            override.sequence += 1
            override.save(update_fields={*values, "sequence"})
        return override
    from .recurrence import split_parts

    first, second = split_parts(master, recurrence_id)
    master.rrule = first
    master.sequence += 1
    master.save(update_fields={"rrule", "sequence", "recurrence_end"})
    following = _copy(master, **{**_shifted(master, recurrence_id), **values})
    following.rrule = second
    following.exdates = [value for value in master.exdates if value >= recurrence_id.isoformat()]
    following.save()
    # Later overrides now belong to the new series, keeping their IDs and content.
    for override in Event.objects.filter(series=master, recurrence_id__gte=recurrence_id):
        override.series = following
        override.save(update_fields={"series"})
    return following


def cancel_occurrence(*, actor: Any, event_id: Any, recurrence_id: Any, expected_revision: int) -> Any:
    """Remove one occurrence (EXDATE); the rest of the series is untouched."""
    master = _master(actor, event_id, expected_revision)
    _occurrence(master, recurrence_id)
    key = recurrence_id.isoformat()
    if key not in master.exdates:
        master.exdates = [*master.exdates, key]
        master.sequence += 1
        master.save(update_fields={"exdates", "sequence", "recurrence_end"})
    return master
