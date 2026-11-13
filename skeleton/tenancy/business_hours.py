"""The business calendar of an organization (#416): weekly hours, holidays and business-hour sums.

``OrganizationSettings.business_hours`` maps a weekday (``"0"`` Monday … ``"6"`` Sunday) to
intervals ``[["09:00", "12:00"], ["13:00", "18:00"]]``; ``holidays`` lists ISO dates. Both are
read in the organization's time zone (its default, else the instance's). No calendar configured
means no business hours: callers fall back to elapsed time.

The text form edited on the settings page is one line per day (``seg 09:00-12:00 13:00-18:00``,
also ``mon``…``sun``) and one holiday date per line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

DAYS = {
    "seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5, "dom": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}  # fmt: skip
LABELS = ("seg", "ter", "qua", "qui", "sex", "sab", "dom")
INTERVAL = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-4]):([0-5]\d)$")
MAX_DAYS = 3660  # Ten years of search: a calendar with no open hour fails instead of looping.
MAX_HOLIDAYS = 500


@dataclass(frozen=True)
class BusinessCalendar:
    zone: ZoneInfo
    hours: dict[int, tuple[tuple[time, time], ...]]
    holidays: frozenset[date]

    def open_on(self, day: date) -> tuple[tuple[time, time], ...]:
        return () if day in self.holidays else self.hours.get(day.weekday(), ())


def _time(text: str) -> time:
    hours, minutes = map(int, text.split(":"))
    return time.max if hours == 24 else time(hours, minutes)


def parse_hours(text: Any) -> dict[str, list[list[str]]]:
    """The stored form of the weekly hours typed on the settings page; ``ValidationError`` on any slip."""
    if not isinstance(text, str):
        raise ValidationError({"business_hours": gettext("Use um texto.")})
    hours: dict[str, list[list[str]]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        parts = line.split()
        if not parts:
            continue
        day = DAYS.get(parts[0].lower().rstrip(":"))
        if day is None or str(day) in hours or len(parts) == 1:
            raise ValidationError(
                {"business_hours": gettext('Linha %(number)s: use "seg 09:00-18:00".') % {"number": number}}
            )
        intervals = []
        previous = None
        for item in parts[1:]:
            match = INTERVAL.match(item)
            if match is None:
                raise ValidationError(
                    {"business_hours": gettext("Linha %(number)s: horário inválido.") % {"number": number}}
                )
            start, end = item.split("-")
            if _time(start) >= _time(end) or (previous is not None and _time(start) < previous):
                raise ValidationError(
                    {
                        "business_hours": gettext("Linha %(number)s: intervalos em ordem e sem sobreposição.")
                        % {"number": number}
                    }
                )
            previous = _time(end)
            intervals.append([start, end])
        hours[str(day)] = intervals
    return hours


def parse_holidays(text: Any) -> list[str]:
    if not isinstance(text, str):
        raise ValidationError({"holidays": gettext("Use um texto.")})
    found = set()
    for line in text.split():
        try:
            found.add(date.fromisoformat(line.strip()).isoformat())
        except ValueError:
            raise ValidationError(
                {"holidays": gettext("Data inválida: %(value)s (use AAAA-MM-DD).") % {"value": line[:20]}}
            ) from None
    if len(found) > MAX_HOLIDAYS:
        raise ValidationError({"holidays": gettext("Use até %(count)s feriados.") % {"count": MAX_HOLIDAYS}})
    return sorted(found)


def format_hours(hours: Any) -> str:
    rows = []
    for day in range(7):
        intervals = (hours or {}).get(str(day)) or []
        if intervals:
            rows.append(" ".join([LABELS[day], *(f"{start}-{end}" for start, end in intervals)]))
    return "\n".join(rows)


def format_holidays(holidays: Any) -> str:
    return "\n".join(holidays or [])


def calendar_of(settings: Any, *, default_timezone: str = "UTC") -> BusinessCalendar | None:
    """The calendar of a settings row; ``None`` when no business hour is configured."""
    if settings is None or not getattr(settings, "business_hours", None):
        return None
    hours = {
        int(day): tuple((_time(start), _time(end)) for start, end in intervals)
        for day, intervals in settings.business_hours.items()
        if intervals
    }
    if not hours:
        return None
    zone = ZoneInfo(settings.default_timezone or default_timezone)
    return BusinessCalendar(zone, hours, frozenset(date.fromisoformat(item) for item in settings.holidays or ()))


def organization_calendar() -> BusinessCalendar | None:
    """The calendar of the context organization (``None`` when it has none)."""
    from django.conf import settings as django_settings

    from .organization_settings import current_settings

    return calendar_of(current_settings(), default_timezone=getattr(django_settings, "TIME_ZONE", "UTC") or "UTC")


def add_business_hours(start: datetime, hours: float, calendar: BusinessCalendar) -> datetime:
    """``start`` plus ``hours`` of open time in ``calendar``, as an aware UTC datetime."""
    remaining = timedelta(hours=hours)
    cursor = start.astimezone(calendar.zone)
    day = cursor.date()
    for _ in range(MAX_DAYS):
        for opening, closing in calendar.open_on(day):
            begin = max(cursor, datetime.combine(day, opening, tzinfo=calendar.zone))
            end = (
                datetime.combine(day + timedelta(days=1), time.min, tzinfo=calendar.zone)
                if closing == time.max
                else datetime.combine(day, closing, tzinfo=calendar.zone)
            )
            if begin >= end:
                continue
            available = end - begin
            if remaining <= available:
                return (begin + remaining).astimezone(UTC)
            remaining -= available
        day += timedelta(days=1)
        cursor = datetime.combine(day, time.min, tzinfo=calendar.zone)
    raise ValueError("The business calendar has no open hour within the search window.")
