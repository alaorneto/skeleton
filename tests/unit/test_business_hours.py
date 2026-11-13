"""Business-hour sums of an organization calendar (#416): no database."""

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError

from skeleton.tenancy.business_hours import (
    add_business_hours,
    calendar_of,
    format_hours,
    parse_holidays,
    parse_hours,
)

WEEKDAYS = "\n".join(f"{day} 09:00-18:00" for day in ("seg", "ter", "qua", "qui", "sex"))


def calendar(hours=WEEKDAYS, holidays="", zone="America/Sao_Paulo"):
    row = SimpleNamespace(business_hours=parse_hours(hours), holidays=parse_holidays(holidays), default_timezone=zone)
    return calendar_of(row)


def local(text, zone="America/Sao_Paulo"):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone))


def test_eight_business_hours_from_friday_afternoon_end_on_monday():
    due = add_business_hours(local("2026-11-13T16:00"), 8, calendar())  # A Friday.
    assert due == local("2026-11-16T15:00").astimezone(UTC)
    assert add_business_hours(local("2026-11-14T10:00"), 1, calendar()) == local("2026-11-16T10:00").astimezone(UTC)


def test_holidays_and_lunch_breaks_are_skipped():
    split = calendar("seg 09:00-12:00 13:00-18:00", holidays="2026-11-16")
    due = add_business_hours(local("2026-11-16T08:00"), 4, split)  # Monday is a holiday: next Monday.
    assert due == local("2026-11-23T14:00").astimezone(UTC)


def test_daylight_saving_changes_keep_wall_clock_hours():
    lisbon = calendar(WEEKDAYS + "\nsab 09:00-18:00\ndom 09:00-18:00", zone="Europe/Lisbon")
    due = add_business_hours(local("2026-03-28T17:00", "Europe/Lisbon"), 2, lisbon)  # DST starts on the 29th.
    assert due == local("2026-03-29T10:00", "Europe/Lisbon").astimezone(UTC)


def test_the_text_form_round_trips_and_rejects_slips():
    assert format_hours(parse_hours("SEG 09:00-12:00 13:00-18:00\nsex 08:00-12:00")) == (
        "seg 09:00-12:00 13:00-18:00\nsex 08:00-12:00"
    )
    assert calendar_of(SimpleNamespace(business_hours={}, holidays=[], default_timezone="")) is None
    for wrong in (
        "seg",
        "xyz 09:00-10:00",
        "seg 10:00-09:00",
        "seg 09:00-12:00 11:00-13:00",
        "seg 9-10",
        "seg 09:00-10:00\nseg 11:00-12:00",
    ):
        with pytest.raises(ValidationError):
            parse_hours(wrong)
    with pytest.raises(ValidationError):
        parse_holidays("2026-02-30")
    assert parse_holidays("2026-12-25\n2026-01-01 2026-12-25") == ["2026-01-01", "2026-12-25"]
