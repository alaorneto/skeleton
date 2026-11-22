"""Calendar services (#68): every write of the app goes through here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

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
        organization_id=current_context().organization_id,  # type: ignore[misc]  # Set: tenant context.
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


def create_calendar_record(record: Calendar, *, user: Any, values: dict[str, Any]) -> None:
    """Calendars created through the table gateway are shared calendars of the organization."""
    record.kind = Calendar.Kind.SHARED
    record.is_default = False
    record.save()


def _refuse_managed(record: Calendar) -> None:
    """A calendar managed by another app changes only in that app (#670): 409 everywhere else."""
    if record.source_app:
        from skeleton.service_api import Conflict

        raise Conflict("managed_calendar", gettext("Este calendário é gerenciado por outra app; altere-o na origem."))


def _refuse_managed_record(record: Calendar) -> None:
    """The table gateway (REST and generic pages) refuses a managed calendar as invalid input."""
    if record.source_app:
        raise ValidationError(
            {"source_app": gettext("Este calendário é gerenciado por outra app; altere-o na origem.")}
        )


def update_calendar_record(record: Calendar, *, user: Any, values: dict[str, Any]) -> None:
    _refuse_managed_record(record)
    for name, value in values.items():
        setattr(record, name, value)
    record.save(update_fields=set(values))


def delete_calendar_record(record: Calendar, *, user: Any) -> None:
    _refuse_managed_record(record)
    if record.kind == Calendar.Kind.PERSONAL and record.is_default:
        raise ValidationError({"kind": gettext("O calendário pessoal padrão não pode ser excluído.")})
    record.delete()


def restore_calendar_record(record: Calendar, *, user: Any) -> None:
    record.restore()


# --------------------------------------------------------------------------- calendar pages (#670)

SHARE_ACTIONS = {"read": ("read",), "write": ("read", "update")}


@dataclass(frozen=True, slots=True)
class Sharing:
    """Who reads and who writes a calendar by record grant: groups and people (#670)."""

    reader_group_ids: frozenset[UUID] = frozenset()
    writer_group_ids: frozenset[UUID] = frozenset()
    reader_user_ids: frozenset[UUID] = frozenset()
    writer_user_ids: frozenset[UUID] = frozenset()


def _subject_actions(actor: Any, calendar: Calendar) -> dict[tuple[str, UUID], set[str]]:
    from skeleton.service_api.access import AccessService

    from .policies import TABLE_KEY

    actions: dict[tuple[str, UUID], set[str]] = {}
    for grant in AccessService().record_grants(actor=actor, table_key=TABLE_KEY, record_id=calendar.pk):
        if grant.action not in ("read", "update"):
            continue  # Delete grants and role grants stay as the access management left them.
        if grant.group_id is not None:
            actions.setdefault(("group", grant.group_id), set()).add(grant.action)
        elif grant.membership_user_id is not None:
            actions.setdefault(("user", grant.membership_user_id), set()).add(grant.action)
    return actions


def calendar_sharing(*, actor: Any, calendar: Calendar) -> Sharing:
    """The sharing of a calendar as the page shows it; ``access.manage`` (Forbidden otherwise)."""
    actions = _subject_actions(actor, calendar)
    pick = {
        (kind, level): frozenset(
            subject
            for (subject_kind, subject), granted in actions.items()
            if subject_kind == kind and ("update" in granted) == (level == "write")
        )
        for kind in ("group", "user")
        for level in ("read", "write")
    }
    return Sharing(pick["group", "read"], pick["group", "write"], pick["user", "read"], pick["user", "write"])


def set_calendar_sharing(*, actor: Any, calendar: Calendar, sharing: Sharing) -> bool:
    """Grant and revoke read and update so that the calendar is shared exactly as ``sharing`` says.

    Only shared calendars that no app manages; ``access.manage`` through the core grant service,
    so each change is audited and holds on the next request. Returns whether anything changed.
    """
    from skeleton.service_api import InvalidInput
    from skeleton.service_api.access import AccessService

    from .policies import TABLE_KEY

    _refuse_managed(calendar)
    if calendar.kind != Calendar.Kind.SHARED:
        raise InvalidInput({"calendar": [gettext("Um calendário pessoal é delegado pela gestão de acesso.")]})
    desired: dict[tuple[str, UUID], set[str]] = {}
    for kind, level, subjects in (
        ("group", "read", sharing.reader_group_ids),
        ("group", "write", sharing.writer_group_ids),
        ("user", "read", sharing.reader_user_ids),
        ("user", "write", sharing.writer_user_ids),
    ):
        for subject in subjects:
            desired.setdefault((kind, subject), set()).update(SHARE_ACTIONS[level])
    if len(desired) > 50:
        raise InvalidInput({"sharing": [gettext("Compartilhe com até 50 grupos e pessoas.")]})
    current = _subject_actions(actor, calendar)
    access = AccessService()
    changed = False

    def subject_of(kind: str, subject: UUID) -> dict[str, UUID]:
        return {"group_id": subject} if kind == "group" else {"membership_user_id": subject}

    with transaction.atomic(using=current_context().using):
        for (kind, subject), actions in current.items():
            for action in sorted(actions - desired.get((kind, subject), set())):
                access.revoke_record_access(
                    actor=actor, table_key=TABLE_KEY, record_id=calendar.pk, action=action, **subject_of(kind, subject)
                )
                changed = True
        for (kind, subject), actions in desired.items():
            for action in sorted(actions - current.get((kind, subject), set())):
                access.grant_record_access(
                    actor=actor, table_key=TABLE_KEY, record_id=calendar.pk, action=action, **subject_of(kind, subject)
                )
                changed = True
    return changed


def rename_calendar(*, actor: Any, calendar_id: Any, name: str, expected_revision: int) -> Calendar:
    """Rename a calendar the actor updates, with CAS; a managed calendar answers 409 (#670)."""
    from skeleton.service_api import Conflict

    from .policies import can_write

    with transaction.atomic(using=current_context().using):
        calendar = Calendar.objects.select_for_update().filter(pk=calendar_id).first()
        if calendar is None or not can_write(actor, calendar):
            raise PermissionDenied(gettext("Calendário indisponível."))
        _refuse_managed(calendar)
        if calendar.revision != expected_revision:
            raise Conflict("stale_revision", gettext("O calendário mudou."))
        calendar.name = name
        calendar.save(update_fields={"name"})
    return calendar


def delete_calendar(*, actor: Any, calendar_id: Any, expected_revision: int) -> None:
    """Soft delete a shared calendar the actor may delete, with CAS; managed ones answer 409 (#670).

    A calendar with live events is refused (``Conflict("live_references")``): its events go first.
    """
    from skeleton.access.policies import require_record_action
    from skeleton.service_api import Conflict

    from .models import Event

    with transaction.atomic(using=current_context().using):
        calendar = Calendar.objects.select_for_update().filter(pk=calendar_id).first()
        if calendar is None:
            raise PermissionDenied(gettext("Calendário indisponível."))
        require_record_action(calendar, user=actor, action="delete")
        _refuse_managed(calendar)
        if calendar.kind == Calendar.Kind.PERSONAL and calendar.is_default:
            raise ValidationError({"kind": gettext("O calendário pessoal padrão não pode ser excluído.")})
        if calendar.revision != expected_revision:
            raise Conflict("stale_revision", gettext("O calendário mudou."))
        if Event.objects.filter(calendar=calendar).exists():
            raise Conflict("live_references", gettext("O calendário tem eventos."))
        calendar.delete()


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
COPIED_FIELDS = EVENT_FIELDS[:-3]  # Without rrule, rdates and exdates; tzid once (#345).


def _event_values(data: dict[str, Any]) -> dict[str, Any]:
    unknown = set(data) - set(EVENT_FIELDS)
    if unknown:
        raise ValidationError({"__all__": gettext("Campos não aceitos: %(value1)s.") % {"value1": sorted(unknown)}})
    values = dict(data)
    if values.get("all_day"):
        values["starts_at"] = values["ends_at"] = None
    elif "all_day" in values:
        values["start_date"] = values["end_date"] = None
    return values


def create_event(*, actor: Any, calendar_id: Any, data: dict[str, Any], uid: str | None = None) -> Any:
    """Create an event in a calendar the actor may update; the actor is the organizer."""
    from .models import Event
    from .policies import require_writable_calendar

    calendar = require_writable_calendar(actor, calendar_id)
    values = _event_values(data)
    values.setdefault("tzid", calendar.timezone)
    if uid is not None:
        values["uid"] = uid
    event = Event(  # type: ignore[misc]  # The organization is set: this runs in a tenant context.
        organization_id=current_context().organization_id, calendar=calendar, organizer_id=actor.pk, **values
    )
    event.save()
    return event


def refuse_managed(event: Any) -> None:
    """A managed event (#537) changes only through the service of the app that owns it."""
    if event is not None and event.source_app:
        from skeleton.service_api import Conflict

        raise Conflict("managed_event", gettext("Este evento é gerenciado por outra app; altere-o na origem."))


def update_event(*, actor: Any, event_id: Any, data: dict[str, Any], expected_revision: int) -> Any:
    """Change an event with CAS; every change bumps the iCalendar ``sequence``."""
    from skeleton.service_api import Conflict

    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    refuse_managed(event)
    if type(expected_revision) is not int or event.revision != expected_revision:
        raise Conflict("stale_revision", gettext("O evento mudou."))
    values = _event_values(data)
    if not values:
        # An empty change would bump revision and sequence and notify for nothing (#294).
        raise ValidationError({"__all__": gettext("Informe ao menos um campo.")})
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
    """Soft delete the event with its reminders, attendees and overrides, one row at a time (#292).

    The attendees are told first; each ``delete()`` is a soft delete audited by the trigger.
    """
    from skeleton.service_api import Conflict

    from .attendees import CANCELLED, notify_attendees
    from .models import Event, EventAttendee, EventReminder
    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    refuse_managed(event)
    if type(expected_revision) is not int or event.revision != expected_revision:
        raise Conflict("stale_revision", gettext("O evento mudou."))
    with transaction.atomic(using=current_context().using):
        notify_attendees(event, CANCELLED, actor=actor)  # Before the invitations go.
        rows = [event, *Event.objects.filter(series=event)] if event.series_id is None else [event]
        for row in rows:
            for reminder in EventReminder.objects.filter(event=row):
                reminder.delete()
            for attendee in EventAttendee.objects.filter(event=row):
                attendee.delete()
        for override in rows[1:]:
            override.delete()
        event.delete()


# --------------------------------------------------------------------------- recurrence edits (#70)

EDIT_MODES = ("this", "following", "all")


def _master(actor: Any, event_id: Any, expected_revision: int) -> Any:
    from skeleton.service_api import Conflict

    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    refuse_managed(event)
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


def _exdate_instants(master: Any) -> list[datetime | None]:
    """The instant of each stored EXDATE, ``None`` for an invalid one (kept aligned with the list)."""
    from .recurrence import _instants

    return [next(iter(_instants([value], master)), None) for value in master.exdates]


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


def _shifted(master: Any, recurrence_id: Any) -> dict[str, Any]:
    """Start/end fields of the occurrence that starts at ``recurrence_id`` (same duration)."""
    if master.all_day:
        days = master.end_date - master.start_date
        return {"start_date": recurrence_id.date(), "end_date": recurrence_id.date() + days}
    return {"starts_at": recurrence_id, "ends_at": recurrence_id + (master.ends_at - master.starts_at)}


def edit_occurrence(
    *, actor: Any, event_id: Any, recurrence_id: Any, mode: str, data: dict[str, Any], expected_revision: int
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
        with transaction.atomic(using=current_context().using):
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
            # The master is written too (#294): its end covers the override, and the expected
            # revision is consumed, so a second edit with the same revision is a stale one.
            master.sequence += 1
            master.save(update_fields={"sequence", "recurrence_end"})
        return override
    from .recurrence import split_parts

    with transaction.atomic(using=current_context().using):
        first, second = split_parts(master, recurrence_id)
        following = _copy(master, **{**_shifted(master, recurrence_id), **values})
        following.rrule = second
        # By instant, not by text: a stored item may carry another offset (#345).
        following.exdates = [
            value
            for value, moment in zip(master.exdates, _exdate_instants(master), strict=True)
            if moment is not None and moment >= recurrence_id
        ]
        following.save()
        # Later overrides now belong to the new series, keeping their IDs and content.
        for override in Event.objects.filter(series=master, recurrence_id__gte=recurrence_id):
            override.series = following
            override.save(update_fields={"series"})
        # Each master is written once its overrides are in place, so both ends are right (#294).
        master.rrule = first
        master.sequence += 1
        master.save(update_fields={"rrule", "sequence", "recurrence_end"})
        following.save(update_fields={"recurrence_end"})
    return following


def cancel_occurrence(*, actor: Any, event_id: Any, recurrence_id: Any, expected_revision: int) -> Any:
    """Remove one occurrence (EXDATE); the rest of the series is untouched."""
    master = _master(actor, event_id, expected_revision)
    _occurrence(master, recurrence_id)
    key = recurrence_id.astimezone(UTC).isoformat()  # The normalized form Event.clean stores (#236).
    if key not in master.exdates:
        master.exdates = [*master.exdates, key]
        master.sequence += 1
        master.save(update_fields={"exdates", "sequence", "recurrence_end"})
    return master


# --------------------------------------------------------------------------- managed events (#537)

MANAGED_FIELDS = (
    "title",
    "location",
    "all_day",
    "starts_at",
    "ends_at",
    "start_date",
    "end_date",
    "tzid",
    "status",
    "is_private",
)
MANAGED_KEY = r"[A-Za-z0-9:._-]{1,200}"


def managed_values(data: Any) -> dict[str, Any]:
    """The accepted fields of a managed event; ``description`` is always refused (#537)."""
    if not isinstance(data, dict):
        raise ValidationError({"data": gettext("Envie os campos do evento.")})
    if "description" in data:
        raise ValidationError(
            {"description": gettext("Um evento gerenciado não tem descrição; o detalhe fica na origem.")}
        )
    unknown = set(data) - set(MANAGED_FIELDS)
    if unknown:
        raise ValidationError({"__all__": gettext("Campos não aceitos: %(value1)s.") % {"value1": sorted(unknown)}})
    return _event_values(data)


def managed_uid(source_app: str, source_key: str, calendar_id: Any) -> str:
    import hashlib

    return hashlib.sha256(f"{source_app}:{source_key}:{calendar_id}".encode()).hexdigest() + "@managed"


def managed_event_row(source_app: str, source_key: str, *, lock: bool = False) -> Any:
    from .models import Event

    rows = Event.objects.select_related("calendar").filter(source_app=source_app, source_key=source_key)
    return (rows.select_for_update(of=("self",)) if lock else rows).first()


def remove_event_rows(event: Any) -> None:
    """Soft delete a managed event with its attendees and reminders, silently (#537)."""
    from .models import EventAttendee, EventReminder

    for reminder in EventReminder.objects.filter(event=event):
        reminder.delete()
    for attendee in EventAttendee.objects.filter(event=event):
        attendee.delete()
    event.delete()


def _create_managed(
    actor: Any, calendar: Any, source_app: str, source_key: str, source_path: str, values: dict[str, Any]
) -> tuple[Any, bool]:
    """Insert the managed event; a concurrent insert of the same key is read back instead (#537)."""
    from .models import Event

    event = Event(  # type: ignore[misc]  # The organization is set: this runs in a tenant context.
        organization_id=current_context().organization_id,
        calendar=calendar,
        organizer_id=actor.pk if actor is not None else calendar.owner_id,
        uid=managed_uid(source_app, source_key, calendar.pk),
        source_app=source_app,
        source_key=source_key,
        source_path=source_path,
        **{"is_private": True, "tzid": calendar.timezone, **values},
    )
    try:
        with transaction.atomic(using=current_context().using):
            event.save()
    except (IntegrityError, ValidationError):
        winner = managed_event_row(source_app, source_key, lock=True)
        if winner is None or winner.calendar_id != calendar.pk or winner.source_path != source_path:
            raise
        return winner, False
    return event, True


def put_managed_event(
    *,
    actor: Any,
    calendar: Any,
    source_app: str,
    source_key: str,
    source_path: str,
    data: dict[str, Any],
    attendee_user_ids: tuple[Any, ...],
) -> Any:
    """Create or update the one live event of ``(source_app, source_key)``; idempotent, never notifies.

    The caller has authorized the calendar. Moving to another calendar deletes the old event and
    creates a new one (the calendar is immutable). A repeat with the same data changes nothing.
    """
    from .attendees import sync_managed_attendees

    values = managed_values(data)
    using = current_context().using
    with transaction.atomic(using=using):
        event = managed_event_row(source_app, source_key, lock=True)
        if event is not None and (event.calendar_id != calendar.pk or event.source_path != source_path):
            # Calendar and path are immutable: a new projection of the record replaces the old one.
            remove_event_rows(event)
            event = None
        if event is None:
            event, created = _create_managed(actor, calendar, source_app, source_key, source_path, values)
        else:
            created = False
        if not created:
            changed = {name: value for name, value in values.items() if getattr(event, name) != value}
            if changed:
                for name, value in changed.items():
                    setattr(event, name, value)
                event.sequence += 1
                event.save(update_fields={*changed, "sequence", "recurrence_end"})
        sync_managed_attendees(event, attendee_user_ids, actor=actor)
    return event
