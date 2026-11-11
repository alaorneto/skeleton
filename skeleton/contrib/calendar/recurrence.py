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
- Edits apply to "only this" (an override), "this and following" (the series is split: the
  first part ends before the occurrence, a new master starts there, and later overrides move to
  it keeping their IDs) or "all" (the master).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import rrule as dateutil_rrule
from django.core.exceptions import ValidationError

MAX_COUNT = 1000
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
        raise _invalid("Informe a regra de recorrência.")
    value = text.strip().upper()
    if value.startswith("RRULE:"):
        value = value[6:]
    parts: dict[str, str] = {}
    for item in value.split(";"):
        name, sep, part = item.partition("=")
        if not sep or name in parts:
            raise _invalid("Regra malformada.")
        if name not in PARTS:
            raise _invalid(f"{name} não é aceito; use FREQ, INTERVAL, COUNT, UNTIL e BY* simples.")
        if not _VALUE[name].match(part):
            raise _invalid(f"Valor inválido em {name}.")
        parts[name] = part
    if "FREQ" not in parts:
        raise _invalid("FREQ é obrigatório (DAILY, WEEKLY, MONTHLY ou YEARLY).")
    if ("COUNT" in parts) == ("UNTIL" in parts):
        raise _invalid("Limite a série com COUNT ou UNTIL (um dos dois).")
    if "COUNT" in parts and int(parts["COUNT"]) > MAX_COUNT:
        raise _invalid(f"COUNT vai até {MAX_COUNT}.")
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


def _rule(event: Any, parts: dict[str, str]) -> dateutil_rrule.rrule:
    zone = ZoneInfo(event.tzid)
    kwargs: dict[str, Any] = {"dtstart": local_start(event), "interval": int(parts.get("INTERVAL", "1"))}
    if "COUNT" in parts:
        kwargs["count"] = int(parts["COUNT"])
    if "UNTIL" in parts:
        kwargs["until"] = _until(parts["UNTIL"], zone, all_day=event.all_day)
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
    return dateutil_rrule.rrule(FREQUENCIES[parts["FREQ"]], **kwargs)


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
    rule = _rule(event, parts)
    if "COUNT" in parts:
        last = list(rule)[-1]
    else:
        last = rule.before(_until(parts["UNTIL"], ZoneInfo(event.tzid), all_day=event.all_day), inc=True)
        if last is None:
            last = local_start(event)
    extra = [_instant(value, event) for value in event.rdates]
    end = occurrence_key(event, last) + _duration(event)
    return max([end, *[moment + _duration(event) for moment in extra]])


def expand(event: Any, start: datetime, end: datetime, *, overrides=None, limit: int = MAX_OCCURRENCES) -> Expansion:
    """Occurrences of ``event`` that overlap ``[start, end)``, overrides applied."""
    if end <= start or end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise ValidationError({"window": f"Use uma janela de até {MAX_WINDOW_DAYS} dias."})
    duration = _duration(event)
    result = Expansion()
    if not event.rrule:
        first = occurrence_key(event, local_start(event))
        if first < end and first + duration > start:
            result.occurrences.append(Occurrence(event, None, first, first + duration))
        return result
    replaced = {row.recurrence_id: row for row in (overrides or ())}
    excluded = {_instant(value, event) for value in event.exdates}
    zone_start = (start - duration).astimezone(ZoneInfo(event.tzid)).replace(tzinfo=None)
    zone_end = end.astimezone(ZoneInfo(event.tzid)).replace(tzinfo=None)
    rule = _rule(event, parse_rule(event.rrule))
    keys = [occurrence_key(event, local) for local in rule.between(zone_start, zone_end, inc=True)]
    keys += [_instant(value, event) for value in event.rdates]
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
    rule = _rule(event, parts)
    local_split = (
        occurrence_key_value.astimezone(zone).replace(tzinfo=None)
        if not event.all_day
        else (datetime.combine(occurrence_key_value.date(), time()))
    )
    before = rule.before(local_split, inc=False)
    if before is None:
        raise ValidationError({"recurrence_id": "Para dividir na primeira ocorrência, edite a série inteira."})
    first = dict(parts)
    second = dict(parts)
    if "COUNT" in parts:
        done = len(list(rule.between(local_start(event), local_split, inc=True))) - 1
        first["COUNT"] = str(done)
        second["COUNT"] = str(int(parts["COUNT"]) - done)
    else:
        first.pop("UNTIL")
        first.pop("COUNT", None)
        until = before.replace(tzinfo=zone).astimezone(UTC) if not event.all_day else before
        first["UNTIL"] = until.strftime("%Y%m%dT%H%M%SZ") if not event.all_day else until.strftime("%Y%m%d")
    return format_rule(first), format_rule(second)
