"""Service API of the calendar (docs/service-api.md): the only module other apps may import."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from skeleton.access.policies import authorized_queryset
from skeleton.service_api import ServiceAPI, service_method

from .models import Calendar

APP_LABEL = "skeleton_calendar"


@dataclass(frozen=True, slots=True)
class CalendarInfo:
    sysid: UUID
    name: str
    kind: str
    is_default: bool
    color: str
    timezone: str
    owner_id: UUID
    revision: int


def calendar_info(calendar: Calendar) -> CalendarInfo:
    return CalendarInfo(
        sysid=calendar.pk,
        name=calendar.name,
        kind=calendar.kind,
        is_default=calendar.is_default,
        color=calendar.color,
        timezone=calendar.timezone,
        owner_id=calendar.owner_id,
        revision=calendar.revision,
    )


class CalendarService(ServiceAPI):
    """Calendars and events of the current organization, authorized for the actor."""

    app_label = APP_LABEL

    @service_method(read_only=False, since="0.5.0")
    def my_calendars(self, *, actor: Any) -> tuple[CalendarInfo, ...]:
        """Calendars the actor reads in the current organization (personal one created on demand)."""
        from .services import ensure_personal_calendar

        ensure_personal_calendar(actor)
        rows = authorized_queryset(Calendar, user=actor, action="read").order_by("-is_default", "name", "sysid")
        return tuple(calendar_info(calendar) for calendar in rows)
