"""Recurrence (#70): local wall clock across DST, EXDATE, overrides, limits and splitting."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.exceptions import ValidationError

from skeleton.contrib.calendar import services
from skeleton.contrib.calendar.models import Calendar, Event
from skeleton.contrib.calendar.recurrence import expand, validate_rule
from tools.security_fixtures import actor_tenant


def calendar_of(agenda):
    with actor_tenant(agenda.alice, agenda.a):
        calendar = Calendar(organization_id=agenda.a.pk, owner=agenda.alice, name="Equipe", kind="shared")
        calendar.save()
    return calendar


def weekly(agenda, calendar, *, tzid, start_local, rule):
    zone = ZoneInfo(tzid)
    starts_at = start_local.replace(tzinfo=zone).astimezone(UTC)
    with actor_tenant(agenda.alice, agenda.a):
        return services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={
                "title": "Semanal",
                "starts_at": starts_at,
                "ends_at": starts_at + timedelta(hours=1),
                "tzid": tzid,
                "rrule": rule,
            },
        )


@pytest.mark.parametrize(
    ("tzid", "start_local"),
    [
        ("Europe/Berlin", datetime(2026, 3, 16, 9, 0)),  # DST starts on 2026-03-29.
        ("America/Sao_Paulo", datetime(2018, 10, 22, 9, 0)),  # Historic DST started on 2018-11-04.
    ],
)
def test_weekly_event_keeps_its_local_hour_across_dst(agenda, tzid, start_local):
    calendar = calendar_of(agenda)
    event = weekly(agenda, calendar, tzid=tzid, start_local=start_local, rule="FREQ=WEEKLY;COUNT=6")
    zone = ZoneInfo(tzid)
    start = start_local.replace(tzinfo=zone).astimezone(UTC)
    occurrences = expand(event, start - timedelta(days=1), start + timedelta(days=60)).occurrences
    assert len(occurrences) == 6
    assert {occurrence.starts_at.astimezone(zone).hour for occurrence in occurrences} == {9}
    offsets = {occurrence.starts_at.astimezone(zone).utcoffset() for occurrence in occurrences}
    assert len(offsets) == 2  # The UTC instant moved with the clock change.
    assert event.recurrence_end == occurrences[-1].ends_at


def test_exdate_removes_and_override_moves_one_occurrence(agenda):
    calendar = calendar_of(agenda)
    event = weekly(
        agenda, calendar, tzid="Europe/Berlin", start_local=datetime(2026, 5, 4, 9, 0), rule="FREQ=WEEKLY;COUNT=4"
    )
    window = (event.starts_at - timedelta(days=1), event.starts_at + timedelta(days=40))
    second, third = [occurrence.recurrence_id for occurrence in expand(event, *window).occurrences][1:3]
    with actor_tenant(agenda.alice, agenda.a):
        event = services.cancel_occurrence(
            actor=agenda.alice, event_id=event.pk, recurrence_id=second, expected_revision=event.revision
        )
        moved = services.edit_occurrence(
            actor=agenda.alice,
            event_id=event.pk,
            recurrence_id=third,
            mode="this",
            data={"starts_at": third + timedelta(hours=2), "ends_at": third + timedelta(hours=3), "title": "Movida"},
            expected_revision=event.revision,
        )
        overrides = list(Event.objects.filter(series=event))
    result = expand(event, *window, overrides=overrides).occurrences
    assert [occurrence.recurrence_id for occurrence in result] == [event.starts_at, third, result[-1].recurrence_id]
    assert second not in {occurrence.recurrence_id for occurrence in result}
    moved_row = next(occurrence for occurrence in result if occurrence.recurrence_id == third)
    assert moved_row.event.pk == moved.pk and moved_row.starts_at == third + timedelta(hours=2)
    assert moved.uid == event.uid and moved.series_id == event.pk


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=SECONDLY;COUNT=5",
        "FREQ=HOURLY;COUNT=5",
        "FREQ=DAILY",  # Unbounded.
        "FREQ=DAILY;COUNT=1001",
        "FREQ=DAILY;COUNT=2;UNTIL=20270101T000000Z",
        "FREQ=WEEKLY;COUNT=2;BYHOUR=9",
        "FREQ=WEEKLY;COUNT=2;BYDAY=XX",
        "nonsense",
    ],
)
def test_rules_outside_the_subset_are_refused(rule):
    with pytest.raises(ValidationError):
        validate_rule(rule)


def test_expansion_is_bounded_and_flags_truncation(agenda):
    calendar = calendar_of(agenda)
    event = weekly(agenda, calendar, tzid="UTC", start_local=datetime(2026, 1, 1, 8, 0), rule="FREQ=DAILY;COUNT=1000")
    result = expand(event, event.starts_at, event.starts_at + timedelta(days=400), limit=50)
    assert len(result.occurrences) == 50 and result.truncated
    full = expand(event, event.starts_at, event.starts_at + timedelta(days=30))
    assert len(full.occurrences) == 30 and not full.truncated
    with pytest.raises(ValidationError):
        expand(event, event.starts_at, event.starts_at + timedelta(days=401))


def test_this_and_following_splits_the_series_keeping_ids(agenda):
    calendar = calendar_of(agenda)
    event = weekly(
        agenda, calendar, tzid="America/Sao_Paulo", start_local=datetime(2026, 6, 1, 10, 0), rule="FREQ=WEEKLY;COUNT=6"
    )
    window = (event.starts_at - timedelta(days=1), event.starts_at + timedelta(days=50))
    keys = [occurrence.recurrence_id for occurrence in expand(event, *window).occurrences]
    with actor_tenant(agenda.alice, agenda.a):
        late = services.edit_occurrence(
            actor=agenda.alice,
            event_id=event.pk,
            recurrence_id=keys[4],
            mode="this",
            data={"title": "Exceção"},
            expected_revision=event.revision,
        )
        event.refresh_from_db()
        following = services.edit_occurrence(
            actor=agenda.alice,
            event_id=event.pk,
            recurrence_id=keys[2],
            mode="following",
            data={"title": "Nova fase"},
            expected_revision=event.revision,
        )
        event.refresh_from_db()
        late.refresh_from_db()
    first_half = expand(event, *window).occurrences
    second_half = expand(following, *window, overrides=[late]).occurrences
    assert [occurrence.recurrence_id for occurrence in first_half] == keys[:2]
    assert [occurrence.recurrence_id for occurrence in second_half] == keys[2:]
    assert event.pk != following.pk and following.uid != event.uid and following.title == "Nova fase"
    assert late.series_id == following.pk and late.title == "Exceção"  # Same row, moved to the new series.
    assert "COUNT=2" in event.rrule and "COUNT=4" in following.rrule


# --------------------------------------------------------------------------- bounded cost and input (#236)

START = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)


def fake_event(rule="", **changes):
    from types import SimpleNamespace

    values = dict(
        all_day=False,
        starts_at=START,
        ends_at=START + timedelta(hours=1),
        start_date=None,
        end_date=None,
        tzid="America/Sao_Paulo",
        rrule=rule,
        rdates=[],
        exdates=[],
        pk="fake",
        deleted_at=None,
    )
    return SimpleNamespace(**{**values, **changes})


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=DAILY;UNTIL=99991231",
        "FREQ=DAILY;UNTIL=18991231T000000Z",
        "FREQ=DAILY;UNTIL=20260231",
        "FREQ=DAILY;UNTIL=20261301T000000Z",
    ],
)
def test_far_or_impossible_until_is_refused(rule):
    with pytest.raises(ValidationError) as error:
        validate_rule(rule)
    assert "rrule" in error.value.message_dict


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30;COUNT=3",
        "FREQ=YEARLY;BYMONTH=4;BYMONTHDAY=31;UNTIL=21001231",
    ],
)
def test_rule_without_occurrences_is_refused_quickly(rule):
    import time as clock

    from skeleton.contrib.calendar.recurrence import series_end

    began = clock.perf_counter()
    with pytest.raises(ValidationError) as error:
        series_end(fake_event(rule))
    assert clock.perf_counter() - began < 0.5
    assert "rrule" in error.value.message_dict


def test_expand_of_a_sparse_rule_is_bounded():
    import time as clock

    event = fake_event("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;COUNT=3")  # Leap days only.
    began = clock.perf_counter()
    expand(event, START, START + timedelta(days=400))
    assert clock.perf_counter() - began < 0.5


@pytest.mark.parametrize(
    "rule,kwargs",
    [
        ("FREQ=DAILY;UNTIL=21001231", {}),
        ("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;UNTIL=21001231", {"interval": 2, "byweekday": ["MO", "WE"]}),
        ("FREQ=MONTHLY;BYDAY=-1FR;UNTIL=21001231", {"byweekday": [("FR", -1)]}),
        ("FREQ=MONTHLY;BYMONTHDAY=31;UNTIL=21001231", {"bymonthday": [31]}),
        ("FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;UNTIL=21001231", {"bymonth": [2], "bymonthday": [29]}),
        (
            "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;UNTIL=21001231",
            {"byweekday": ["MO", "TU", "WE", "TH", "FR"], "bysetpos": [-1]},
        ),
        (
            "FREQ=WEEKLY;BYDAY=SU,MO;WKST=SU;INTERVAL=2;UNTIL=21001231",
            {"byweekday": ["SU", "MO"], "wkst": "SU", "interval": 2},
        ),
    ],
)
def test_bounded_iteration_matches_dateutil(rule, kwargs):
    from itertools import islice

    from dateutil import rrule as dateutil_rrule

    from skeleton.contrib.calendar.recurrence import FREQUENCIES, local_start, occurrence_starts, parse_rule

    event = fake_event(rule)
    parts = parse_rule(rule)
    start = local_start(event)
    days = []
    for item in kwargs.pop("byweekday", ()):
        name, position = item if isinstance(item, tuple) else (item, None)
        weekday = getattr(dateutil_rrule, name)
        days.append(weekday(position) if position else weekday)
    if days:
        kwargs["byweekday"] = days
    if "wkst" in kwargs:
        kwargs["wkst"] = getattr(dateutil_rrule, kwargs["wkst"])
    reference = dateutil_rrule.rrule(
        FREQUENCIES[parts["FREQ"]],
        dtstart=start,
        until=min(datetime(2100, 12, 31, 23, 59, 59), start + timedelta(days=36525)),
        **kwargs,
    )
    assert list(islice(occurrence_starts(event, parts), 300)) == list(islice(reference, 300))


def test_count_is_applied_after_the_shift():
    from skeleton.contrib.calendar.recurrence import local_start, occurrence_starts

    event = fake_event("FREQ=WEEKLY;COUNT=5")
    starts = list(occurrence_starts(event))
    assert starts == [local_start(event) + timedelta(weeks=week) for week in range(5)]


def test_stored_invalid_exdate_is_skipped():
    event = fake_event("FREQ=DAILY;COUNT=5", exdates=["not-a-date"])
    window = expand(event, START - timedelta(days=1), START + timedelta(days=10))
    assert len(window.occurrences) == 5


def _create(agenda, calendar, **data):
    values = {"title": "Série", "starts_at": START, "ends_at": START + timedelta(hours=1), **data}
    with actor_tenant(agenda.alice, agenda.a):
        return services.create_event(actor=agenda.alice, calendar_id=calendar.pk, data=values)


@pytest.mark.parametrize(
    "data,field",
    [
        ({"rrule": "FREQ=DAILY;COUNT=3", "exdates": ["not-a-date"]}, "exdates"),
        ({"rrule": "FREQ=DAILY;COUNT=3", "rdates": ["2026-02-30T10:00:00"]}, "rdates"),
        ({"rrule": "FREQ=DAILY;COUNT=3", "rdates": ["9999-12-31T23:59:59+00:00"]}, "rdates"),
        ({"rrule": "FREQ=DAILY;COUNT=3", "exdates": [42]}, "exdates"),
        ({"tzid": "Mars/Olympus_Mons"}, "tzid"),
        ({"rrule": "FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30;COUNT=3"}, "rrule"),
    ],
)
def test_invalid_exdates_rdates_and_tzid_answer_validation_error(agenda, data, field):
    calendar = calendar_of(agenda)
    with pytest.raises(ValidationError) as error:
        _create(agenda, calendar, **data)
    assert field in error.value.message_dict
    with actor_tenant(agenda.alice, agenda.a):
        assert not Event.objects.filter(calendar=calendar).exists()


def test_invalid_input_reaches_the_service_as_invalid_input(agenda):
    from skeleton.contrib.calendar.service_api import CalendarService
    from skeleton.service_api import InvalidInput

    calendar = calendar_of(agenda)
    data = {
        "title": "x",
        "starts_at": START,
        "ends_at": START + timedelta(hours=1),
        "rrule": "FREQ=DAILY;UNTIL=99991231",
    }
    with actor_tenant(agenda.alice, agenda.a), pytest.raises(InvalidInput) as error:
        CalendarService().create_event(actor=agenda.alice, calendar_id=calendar.pk, data=data)
    assert "rrule" in error.value.errors


def test_recurrence_dates_are_stored_normalized(agenda):
    calendar = calendar_of(agenda)
    event = _create(agenda, calendar, rrule="FREQ=DAILY;COUNT=3", rdates=["2026-12-05T09:00:00-03:00"])
    with actor_tenant(agenda.alice, agenda.a):
        event.refresh_from_db()
    assert event.rdates == ["2026-12-05T12:00:00+00:00"]


def test_event_dates_outside_the_accepted_years_are_refused(agenda):
    calendar = calendar_of(agenda)
    far = datetime(2300, 1, 1, 12, 0, tzinfo=UTC)
    with pytest.raises(ValidationError) as error:
        _create(agenda, calendar, starts_at=far, ends_at=far + timedelta(hours=1))
    assert "starts_at" in error.value.message_dict
