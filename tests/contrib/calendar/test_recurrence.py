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
