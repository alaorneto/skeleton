"""Event authorization derived from the calendar (#69).

- Reading an event needs reading its calendar (``authorized_queryset(Calendar, "read")``);
  attendees (#72) also read the events they are invited to.
- Writing an event needs ``update`` on its calendar; nobody writes through an invitation.
- A private event shows as "Ocupado" to everyone but its organizer and the calendar owner.
- An organization administrator reads every calendar (the admin bypass of ``SecuredRecord``),
  but in a personal calendar of someone else every event shows only as "Ocupado".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.core.exceptions import PermissionDenied
from django.db.models import Q, QuerySet
from django.utils.translation import gettext, gettext_lazy

from skeleton.access.policies import authorized_queryset

from .models import Calendar, Event

if TYPE_CHECKING:
    from uuid import UUID

    from .models import EventAttendee

TABLE_KEY = "skeleton_calendar.calendar"

BUSY_TITLE = gettext_lazy("Ocupado")


def readable_calendars(user: Any) -> QuerySet[Calendar]:
    return authorized_queryset(Calendar, user=user, action="read")


def writable_calendars(user: Any) -> QuerySet[Calendar]:
    return authorized_queryset(Calendar, user=user, action="update")


def attendee_event_ids(user: Any) -> QuerySet[EventAttendee, dict[str, Any]] | None:
    """Events the user is invited to (#72), with an active membership; overrides follow their master."""
    from .models import EventAttendee

    return EventAttendee.objects.filter(
        membership__user_id=user.pk, membership__is_active=True, membership__deleted_at__isnull=True
    ).values("event_id")


def readable_events(user: Any) -> QuerySet[Event]:
    """Events the user may see (as themselves or as "Ocupado"), in the current organization."""
    condition = Q(calendar__in=readable_calendars(user).values("pk"))
    invited = attendee_event_ids(user)
    if invited is not None:
        condition |= Q(pk__in=invited) | Q(series__in=invited)
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
        raise PermissionDenied(gettext("Evento indisponível."))
    return event


@dataclass(frozen=True, slots=True)
class ActorView:
    """What decides masking for one actor, loaded once per call and never kept beyond it (#291)."""

    delegated_calendar_ids: frozenset[UUID]
    invited_event_ids: frozenset[UUID]


def actor_view(user: Any, *, calendar_ids: Any, event_ids: Any) -> ActorView:
    """Delegations on ``calendar_ids`` and invitations to ``event_ids``, in a fixed number of queries."""
    from skeleton.service_api import Forbidden
    from skeleton.service_api.access import AccessService

    calendars = tuple(dict.fromkeys(calendar_ids))
    delegated: set[UUID] = set()
    try:
        for first in range(0, len(calendars), 500):  # The service answers up to 500 identifiers.
            delegated.update(
                AccessService().granted_record_ids(
                    actor=user, table_key=TABLE_KEY, record_ids=calendars[first : first + 500]
                )
            )
    except Forbidden:
        delegated = set()
    events = list(dict.fromkeys(event_ids))
    invited_rows = attendee_event_ids(user)
    invited: frozenset[UUID] = frozenset()
    if invited_rows is not None and events:
        invited = frozenset(invited_rows.filter(event_id__in=events).values_list("event_id", flat=True))
    return ActorView(frozenset(delegated), invited)


def sees_details(user: Any, event: Event, view: ActorView | None = None) -> bool:
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
        if view is not None:
            return calendar.pk in view.delegated_calendar_ids or (event.series_id or event.pk) in view.invited_event_ids
        return is_delegate(user, calendar) or is_attendee(user, event)
    return True


def is_delegate(user: Any, calendar: Calendar, *, action: str = "read") -> bool:
    """An explicit record grant on the calendar, through the core service API (#343)."""
    from skeleton.service_api import Forbidden
    from skeleton.service_api.access import AccessService

    try:
        granted = AccessService().granted_record_ids(
            actor=user, table_key=TABLE_KEY, record_ids=(calendar.pk,), action=action
        )
    except Forbidden:
        return False
    return calendar.pk in granted


def is_attendee(user: Any, event: Event) -> bool:
    invited = attendee_event_ids(user)
    return invited is not None and invited.filter(event_id=event.series_id or event.pk).exists()
