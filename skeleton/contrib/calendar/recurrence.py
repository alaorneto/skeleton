"""Recurrence (#70): a bounded RFC 5545 subset, expanded in local time with ``zoneinfo``.

- Rules: ``FREQ`` in DAILY, WEEKLY, MONTHLY or YEARLY, with ``INTERVAL``, ``BYDAY``,
  ``BYMONTHDAY``, ``BYMONTH``, ``BYSETPOS`` and ``WKST``, and always bounded by ``COUNT`` (at
  most 1000) or ``UNTIL``. Anything else (``FREQ=SECONDLY``, ``BYHOUR``...) is a 400.
- ``DTSTART`` is the master's UTC instant plus its ``tzid``. The rule runs on the local wall
  clock of ``tzid``, so a weekly 09:00 event stays at 09:00 across daylight saving changes; each
  local occurrence is then converted back to UTC. All-day series run on dates.
- ``exdates`` remove occurrences; ``rdates`` add them; an override row (``series`` +
  ``recurrence_id``) replaces one occurrence, and an override with ``is_cancelled`` removes it.
- :func:`expand` reads a window of at most 400 days and returns at most 1000 occurrences,
  flagging ``truncated`` when it stops early.
- Cost is bounded (#236): ``UNTIL`` stays within 1900–2200, a series is never iterated past
  100 years from its start, and a rule whose filters never match is refused on write instead of
  iterating to year 9999 (:func:`occurrence_starts` is the only iteration).
- Edits apply to "only this" (an override), "this and following" (the series is split: the
  first part ends before the occurrence, a new master starts there, and later overrides move to
  it keeping their IDs) or "all" (the master).
"""

from __future__ import annotations

import itertools
import logging
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import rrule as dateutil_rrule
from django.core.exceptions import ValidationError
from django.utils.translation import gettext

logger = logging.getLogger("skeleton.contrib.calendar")
MAX_COUNT = 1000
YEARS = (1900, 2200)  # The same range as ?date= of the pages.
MAX_SPAN = timedelta(days=36525)  # 100 years: no series is expanded beyond this (#236).
_CYCLE = timedelta(days=146097)  # Exactly 400 Gregorian years (and whole weeks).
_LAST = datetime(9999, 12, 31, 23, 59, 59)
MAX_WINDOW_DAYS = 400
MAX_OCCURRENCES = 1000
FREQUENCIES = {
    "DAILY": dateutil_rrule.DAILY,
    "WEEKLY": dateutil_rrule.WEEKLY,
    "MONTHLY": dateutil_rrule.MONTHLY,
    "YEARLY": dateutil_rrule.YEARLY,
}
PARTS = frozenset({"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH", "BYSETPOS", "WKST"})
_DAY = r"(?:[+-]?[1-5])?(?:MO|TU|WE|TH|FR|SA|SU)"
_VALUE = {
    "FREQ": re.compile(r"^(DAILY|WEEKLY|MONTHLY|YEARLY)$"),
    "INTERVAL": re.compile(r"^[1-9][0-9]{0,2}$"),
    "COUNT": re.compile(r"^[1-9][0-9]{0,3}$"),
    "UNTIL": re.compile(r"^[0-9]{8}(T[0-9]{6}Z?)?$"),
    "BYDAY": re.compile(rf"^{_DAY}(,{_DAY})*$"),
    "BYMONTHDAY": re.compile(r"^-?[1-9][0-9]?(,-?[1-9][0-9]?)*$"),
    "BYMONTH": re.compile(r"^(1[0-2]|[1-9])(,(1[0-2]|[1-9]))*$"),
    "BYSETPOS": re.compile(r"^-?[1-9][0-9]{0,2}(,-?[1-9][0-9]{0,2})*$"),
    "WKST": re.compile(r"^(MO|TU|WE|TH|FR|SA|SU)$"),
}


def _invalid(message: str) -> ValidationError:
    return ValidationError({"rrule": message})


def parse_rule(text: str) -> dict[str, str]:
    if not isinstance(text, str) or not text.strip():
        raise _invalid(gettext("Informe a regra de recorrência."))
    value = text.strip().upper()
    if value.startswith("RRULE:"):
        value = value[6:]
    parts: dict[str, str] = {}
    for item in value.split(";"):
        name, sep, part = item.partition("=")
        if not sep or name in parts:
            raise _invalid(gettext("Regra malformada."))
        if name not in PARTS:
            raise _invalid(
                gettext("%(name)s não é aceito; use FREQ, INTERVAL, COUNT, UNTIL e BY* simples.") % {"name": name}
            )
        if not _VALUE[name].match(part):
            raise _invalid(gettext("Valor inválido em %(name)s.") % {"name": name})
        parts[name] = part
    if "FREQ" not in parts:
        raise _invalid(gettext("FREQ é obrigatório (DAILY, WEEKLY, MONTHLY ou YEARLY)."))
    if ("COUNT" in parts) == ("UNTIL" in parts):
        raise _invalid(gettext("Limite a série com COUNT ou UNTIL (um dos dois)."))
    if "COUNT" in parts and int(parts["COUNT"]) > MAX_COUNT:
        raise _invalid(gettext("COUNT vai até %(MAX_COUNT)s.") % {"MAX_COUNT": MAX_COUNT})
    if "UNTIL" in parts:
        text = parts["UNTIL"]
        try:
            moment = (
                datetime.strptime(text[:15], "%Y%m%dT%H%M%S") if len(text) > 8 else datetime.strptime(text, "%Y%m%d")
            )
        except ValueError:
            raise _invalid(gettext("UNTIL não é uma data válida.")) from None
        if not YEARS[0] <= moment.year <= YEARS[1]:
            raise _invalid(
                gettext("UNTIL fora do intervalo aceito (%(first)s a %(last)s).")
                % {"first": YEARS[0], "last": YEARS[1]}
            )
    return parts


def validate_rule(text: str) -> str:
    """The canonical form of an accepted rule (``FREQ`` first, upper case), or ``ValidationError``."""
    parts = parse_rule(text)
    ordered = ["FREQ"] + sorted(name for name in parts if name != "FREQ")
    return ";".join(f"{name}={parts[name]}" for name in ordered)


def format_rule(parts: dict[str, str]) -> str:
    return validate_rule(";".join(f"{name}={value}" for name, value in parts.items()))


def _until(value: str, zone: ZoneInfo, *, all_day: bool) -> datetime:
    if len(value) == 8:
        moment = datetime.strptime(value, "%Y%m%d")
        return moment.replace(hour=23, minute=59, second=59)
    moment = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        # UNTIL in UTC; the rule runs on the local clock, so compare on that clock.
        moment = moment.replace(tzinfo=UTC).astimezone(zone).replace(tzinfo=None)
    return moment


def _rule(event: Any, parts: dict[str, str]) -> tuple[dateutil_rrule.rrule, timedelta]:
    """The dateutil rule moved into the last 400-year cycle ``datetime`` can hold, and the shift.

    dateutil only checks ``until`` when it finds a candidate, so a rule whose filters never
    match would iterate up to year 9999. The Gregorian calendar repeats exactly every 146097
    days: shifting DTSTART by whole cycles keeps every occurrence and bounds the iteration (#236).
    ``COUNT`` is applied by :func:`occurrence_starts`, never passed to dateutil.
    """
    zone = ZoneInfo(event.tzid)
    start = local_start(event)
    shift = _CYCLE * ((9899 - start.year) // 400)
    horizon = start + MAX_SPAN
    until = min(_until(parts["UNTIL"], zone, all_day=event.all_day), horizon) if "UNTIL" in parts else horizon
    kwargs: dict[str, Any] = {
        "dtstart": start + shift,
        "interval": int(parts.get("INTERVAL", "1")),
        "until": min(until + shift, _LAST),
    }
    if "BYDAY" in parts:
        days = []
        for item in parts["BYDAY"].split(","):
            weekday = getattr(dateutil_rrule, item[-2:])
            days.append(weekday(int(item[:-2])) if item[:-2] else weekday)
        kwargs["byweekday"] = days
    for name, key in (("BYMONTHDAY", "bymonthday"), ("BYMONTH", "bymonth"), ("BYSETPOS", "bysetpos")):
        if name in parts:
            kwargs[key] = [int(value) for value in parts[name].split(",")]
    if "WKST" in parts:
        kwargs["wkst"] = getattr(dateutil_rrule, parts["WKST"])
    return dateutil_rrule.rrule(FREQUENCIES[parts["FREQ"]], **kwargs), shift


def occurrence_starts(event: Any, parts: dict[str, str] | None = None) -> Iterator[datetime]:
    """Local naive starts of the series in order; COUNT applied; never past MAX_SPAN after DTSTART.

    The only place the rule is iterated (#236).
    """
    parts = parts if parts is not None else parse_rule(event.rrule)
    rule, shift = _rule(event, parts)
    moments = (moment - shift for moment in rule)
    return itertools.islice(moments, int(parts["COUNT"])) if "COUNT" in parts else moments


def local_start(event: Any) -> datetime:
    """DTSTART on the local wall clock of the event (naive)."""
    if event.all_day:
        return datetime.combine(event.start_date, time())
    return event.starts_at.astimezone(ZoneInfo(event.tzid)).replace(tzinfo=None)


def occurrence_key(event: Any, local: datetime) -> datetime:
    """The UTC instant that identifies an occurrence (its original start: the RECURRENCE-ID)."""
    if event.all_day:
        return datetime.combine(local.date(), time(), tzinfo=UTC)
    return local.replace(tzinfo=ZoneInfo(event.tzid)).astimezone(UTC)


def _duration(event: Any) -> timedelta:
    if event.all_day:
        return timedelta(days=(event.end_date - event.start_date).days)
    return event.ends_at - event.starts_at


def _instant(value: Any, event: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime.combine(value, time(), tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        if event.all_day:
            return parsed.replace(tzinfo=UTC)
        parsed = parsed.replace(tzinfo=ZoneInfo(event.tzid))
    return parsed.astimezone(UTC)


def _instants(values: Any, event: Any) -> list[datetime]:
    """Stored rdates/exdates as UTC instants; an invalid stored item is skipped and logged (#236)."""
    result = []
    for value in values or ():
        try:
            result.append(_instant(value, event))
        except (ValueError, TypeError, OverflowError):
            # Never the value itself: only which event carries it.
            logger.warning(
                "calendar.invalid_recurrence_date",
                extra={"event": "calendar.invalid_recurrence_date", "target_sysid": str(getattr(event, "pk", ""))},
            )
    return result


@dataclass(frozen=True, slots=True)
class Occurrence:
    event: Any  # The master, or the override that replaces this occurrence.
    recurrence_id: datetime | None
    starts_at: datetime  # UTC; midnight UTC of the date for all-day events.
    ends_at: datetime

    @property
    def start_date(self) -> date:
        return self.starts_at.date()


@dataclass
class Expansion:
    occurrences: list[Occurrence] = field(default_factory=list)
    truncated: bool = False


def series_end(event: Any) -> datetime | None:
    """Last instant the series may cover (``recurrence_end``), or ``None`` for an unbounded rule."""
    if not event.rrule:
        return event.ends_at if not event.all_day else datetime.combine(event.end_date, time(), tzinfo=UTC)
    parts = parse_rule(event.rrule)
    try:
        tail = deque(occurrence_starts(event, parts), maxlen=1)
    except OverflowError:
        raise _invalid(gettext("A série ultrapassa o intervalo de datas aceito.")) from None
    if not tail:
        raise _invalid(gettext("A regra não produz nenhuma ocorrência."))
    last = tail[0]
    extra = _instants(event.rdates, event)
    end = occurrence_key(event, last) + _duration(event)
    return max([end, *[moment + _duration(event) for moment in extra]])


def expand(event: Any, start: datetime, end: datetime, *, overrides=None, limit: int = MAX_OCCURRENCES) -> Expansion:
    """Occurrences of ``event`` that overlap ``[start, end)``, overrides applied."""
    if end <= start or end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise ValidationError(
            {
                "window": gettext("Use uma janela de até %(MAX_WINDOW_DAYS)s dias.")
                % {"MAX_WINDOW_DAYS": MAX_WINDOW_DAYS}
            }
        )
    duration = _duration(event)
    result = Expansion()
    if not event.rrule:
        first = occurrence_key(event, local_start(event))
        if first < end and first + duration > start:
            result.occurrences.append(Occurrence(event, None, first, first + duration))
        return result
    replaced = {row.recurrence_id: row for row in (overrides or ())}
    excluded = set(_instants(event.exdates, event))
    zone_start = (start - duration).astimezone(ZoneInfo(event.tzid)).replace(tzinfo=None)
    zone_end = end.astimezone(ZoneInfo(event.tzid)).replace(tzinfo=None)
    keys = []
    for local in occurrence_starts(event, parse_rule(event.rrule)):
        if local > zone_end:
            break
        if local >= zone_start:
            keys.append(occurrence_key(event, local))
    keys += _instants(event.rdates, event)
    for key in sorted(set(keys)):
        if key in excluded:
            continue
        override = replaced.get(key)
        if override is not None:
            if override.is_cancelled or override.deleted_at is not None:
                continue
            first = occurrence_key(override, local_start(override))
            occurrence = Occurrence(override, key, first, first + _duration(override))
        else:
            occurrence = Occurrence(event, key, key, key + duration)
        if occurrence.starts_at >= end or occurrence.ends_at <= start:
            continue
        if len(result.occurrences) >= limit:
            result.truncated = True
            break
        result.occurrences.append(occurrence)
    # Overrides that moved an occurrence into the window from outside it.
    for key, override in replaced.items():
        if key in {occurrence.recurrence_id for occurrence in result.occurrences} or override.is_cancelled:
            continue
        if override.deleted_at is not None or key in excluded:
            continue
        first = occurrence_key(override, local_start(override))
        if first < end and first + _duration(override) > start and len(result.occurrences) < limit:
            result.occurrences.append(Occurrence(override, key, first, first + _duration(override)))
    result.occurrences.sort(key=lambda occurrence: (occurrence.starts_at, str(occurrence.event.pk)))
    return result


def split_parts(event: Any, occurrence_key_value: datetime) -> tuple[str, str]:
    """Rules of the two halves when the series is split at ``occurrence_key_value``."""
    parts = parse_rule(event.rrule)
    zone = ZoneInfo(event.tzid)
    local_split = (
        occurrence_key_value.astimezone(zone).replace(tzinfo=None)
        if not event.all_day
        else (datetime.combine(occurrence_key_value.date(), time()))
    )
    before, done = None, 0
    for local in occurrence_starts(event, parts):
        if local >= local_split:
            break
        before, done = local, done + 1
    if before is None:
        raise ValidationError({"recurrence_id": gettext("Para dividir na primeira ocorrência, edite a série inteira.")})
    first = dict(parts)
    second = dict(parts)
    if "COUNT" in parts:
        first["COUNT"] = str(done)
        second["COUNT"] = str(int(parts["COUNT"]) - done)
    else:
        first.pop("UNTIL")
        first.pop("COUNT", None)
        until = before.replace(tzinfo=zone).astimezone(UTC) if not event.all_day else before
        first["UNTIL"] = until.strftime("%Y%m%dT%H%M%SZ") if not event.all_day else until.strftime("%Y%m%d")
    return format_rule(first), format_rule(second)
