"""iCalendar export (#76): one VEVENT per master (RRULE, RDATE, EXDATE) and per override (RECURRENCE-ID).

What leaves is what the reader may see: a busy block (private event, someone else's personal
calendar without delegation, or the "busy only" option) carries only DTSTART/DTEND, a
"Ocupado" SUMMARY and CLASS:PRIVATE, never the title, description or location.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any
from zoneinfo import ZoneInfo

from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent
from icalendar import vRecur

from .policies import BUSY_TITLE, sees_details

PRODID = "-//Skeleton//Agenda//PT-BR"


def _export_rule(event: Any) -> str:
    """The stored rule with UNTIL as RFC 5545 requires next to a zoned DTSTART (UTC), or a date for all-day (#293)."""
    from .recurrence import _until, parse_rule

    parts = parse_rule(event.rrule)
    if "UNTIL" in parts:
        if event.all_day:
            parts["UNTIL"] = parts["UNTIL"][:8]
        else:
            zone = ZoneInfo(event.tzid)
            local = _until(parts["UNTIL"], zone, all_day=False)  # On the local clock, naive.
            parts["UNTIL"] = local.replace(tzinfo=zone).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return ";".join(f"{name}={value}" for name, value in parts.items())


def vevent(reader: Any, event: Any, *, busy_only: bool = False, view: Any = None) -> IEvent:
    from .recurrence import _instants

    details = not busy_only and sees_details(reader, event, view)
    item = IEvent()
    item.add("uid", event.uid)
    item.add("dtstamp", event.updated_at)
    item.add("sequence", event.sequence)
    item.add("summary", event.title if details else str(BUSY_TITLE))
    if details and event.description:
        item.add("description", event.description)
    if details and event.location:
        item.add("location", event.location)
    if not details or event.is_private:
        item.add("class", "PRIVATE")
    # A series runs on the local clock of its tzid: exported with that TZID (and its VTIMEZONE),
    # a client expands it at the same local hour across daylight saving changes (#293).
    zone = ZoneInfo(event.tzid)
    if event.all_day:
        item.add("dtstart", event.start_date)
        item.add("dtend", event.end_date)
    else:
        item.add("dtstart", event.starts_at.astimezone(zone))
        item.add("dtend", event.ends_at.astimezone(zone))
    item.add("status", event.status.upper())
    if event.rrule:
        item.add("rrule", vRecur.from_ical(_export_rule(event)))
        for name, values in (("exdate", event.exdates), ("rdate", event.rdates)):
            for moment in _instants(values, event):
                item.add(name, moment.date() if event.all_day else moment.astimezone(zone))
    if event.recurrence_id is not None:
        item.add("recurrence-id", event.recurrence_id.date() if event.all_day else event.recurrence_id.astimezone(zone))
    return item


def calendar_bytes(name: str, components: list[IEvent]) -> bytes:
    calendar = ICalendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    calendar.add("x-wr-calname", name)
    for component in components:
        calendar.add_component(component)
    calendar.add_missing_timezones()  # The VTIMEZONE of every TZID used (#293).
    return calendar.to_ical()


def calendar_components(reader: Any, events: Any, *, busy_only: bool = False) -> list[IEvent]:
    """Components of the masters in ``events`` and their overrides, masked for ``reader``.

    A fixed number of queries whatever the number of events (#291).
    """
    from .models import Event
    from .policies import actor_view

    masters = list(
        events.filter(series__isnull=True).select_related("calendar").order_by("starts_at", "start_date", "sysid")
    )
    overrides: dict = {}
    if masters:
        for row in Event.objects.filter(series_id__in=[master.pk for master in masters]).select_related("calendar"):
            overrides.setdefault(row.series_id, []).append(row)
    view = None
    if not busy_only:
        view = actor_view(
            reader, calendar_ids={master.calendar_id for master in masters}, event_ids={master.pk for master in masters}
        )
    rows = []
    for master in masters:
        rows.append(vevent(reader, master, busy_only=busy_only, view=view))
        for override in overrides.get(master.pk, ()):
            rows.append(vevent(reader, override, busy_only=busy_only, view=view))
    return rows


MAX_COMPONENTS = 2000


def capped(components: list[IEvent]) -> list[IEvent]:
    """At most ``MAX_COMPONENTS`` components; a cut is logged without any data of the events (#291)."""
    import logging

    if len(components) > MAX_COMPONENTS:
        logging.getLogger("skeleton.contrib.calendar").warning(
            "calendar.feed_truncated", extra={"event": "calendar.feed_truncated"}
        )
    return components[:MAX_COMPONENTS]
