"""Event authorization derived from the calendar (#69).

- Reading an event needs reading its calendar (``authorized_queryset(Calendar, "read")``);
  attendees (#72) also read the events they are invited to.
- Writing an event needs ``update`` on its calendar; nobody writes through an invitation.
- A private event shows as "Ocupado" to everyone but its organizer and the calendar owner.
- An organization administrator reads every calendar (the admin bypass of ``SecuredRecord``),
  but in a personal calendar of someone else every event shows only as "Ocupado".
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import Q, QuerySet

from skeleton.access.policies import authorized_queryset

from .models import Calendar, Event

BUSY_TITLE = "Ocupado"


def readable_calendars(user: Any) -> QuerySet:
    return authorized_queryset(Calendar, user=user, action="read")


def writable_calendars(user: Any) -> QuerySet:
    return authorized_queryset(Calendar, user=user, action="update")


def attendee_event_ids(user: Any) -> QuerySet | None:
    """Events the user is invited to (#72); ``None`` until attendees exist."""
    return None


def readable_events(user: Any) -> QuerySet:
    """Events the user may see (as themselves or as "Ocupado"), in the current organization."""
    condition = Q(calendar__in=readable_calendars(user).values("pk"))
    invited = attendee_event_ids(user)
    if invited is not None:
        condition |= Q(pk__in=invited)
    return Event.objects.filter(condition)


def can_write(user: Any, calendar: Calendar) -> bool:
    """``update`` on the calendar; in someone else's personal calendar only an update delegation.

    The administrator's bypass of record visibility never writes in a personal calendar.
    """
    if not writable_calendars(user).filter(pk=calendar.pk).exists():
        return False
    if calendar.kind == Calendar.Kind.PERSONAL and calendar.owner_id != user.pk:
        return is_delegate(user, calendar, action="update")
    return True


def require_writable_calendar(user: Any, calendar_id: Any) -> Calendar:
    calendar = Calendar.objects.filter(pk=calendar_id).first()
    if calendar is None or not can_write(user, calendar):
        raise PermissionDenied("Calendar update permission required.")
    return calendar


def require_writable_event(user: Any, event_id: Any) -> Event:
    event = Event.objects.select_related("calendar").filter(pk=event_id).first()
    if event is None or not can_write(user, event.calendar):
        raise PermissionDenied("Event unavailable.")
    return event


def sees_details(user: Any, event: Event) -> bool:
    """Whether ``user`` sees the content of ``event`` or only a busy block.

    The organizer and the calendar owner always see it. A private event is a busy block for
    everyone else. In someone else's personal calendar, only an explicit delegation (a record
    grant on the calendar) or an invitation shows the content: the administrator's bypass of
    record visibility shows busy blocks only.
    """
    calendar = event.calendar
    if calendar.owner_id == user.pk or event.organizer_id == user.pk:
        return True
    if event.is_private:
        return False
    if calendar.kind == Calendar.Kind.PERSONAL:
        return is_delegate(user, calendar) or is_attendee(user, event)
    return True


def is_delegate(user: Any, calendar: Calendar, *, action: str = "read") -> bool:
    from skeleton.access.policies import _grant_ids

    try:
        return _grant_ids(Calendar, user, action).filter(record_sysid=calendar.pk).exists()
    except PermissionDenied:
        return False


def is_attendee(user: Any, event: Event) -> bool:
    invited = attendee_event_ids(user)
    return invited is not None and invited.filter(pk=event.pk).exists()
