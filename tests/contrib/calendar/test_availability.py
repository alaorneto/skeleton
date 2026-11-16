"""Availability with invitations and shared calendars (#538): only times, constant queries, no leak."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from skeleton.contrib.calendar.models import Calendar
from skeleton.contrib.calendar.service_api import BusyBlock, CalendarBusyBlock, CalendarService
from skeleton.service_api import InvalidInput, NotFound
from skeleton.testing.service_api import acting_as, build_service_scenario

START = datetime(2027, 6, 7, 13, 0, tzinfo=UTC)
WINDOW = {"start": START - timedelta(days=1), "end": START + timedelta(days=8)}


@pytest.fixture
def scenario(skeleton_db):
    return build_service_scenario(Calendar, slug="calendar-availability")


def room(scenario, organization=None, **values):
    values = {"name": "Sala 1", "kind": "shared", "visibility": "organization", "owner": scenario.member, **values}
    return scenario.create(organization or scenario.a, **values)


def event(scenario, calendar, *, hours=0, invite=(), **data):
    service = CalendarService()
    moment = START + timedelta(hours=hours)
    with acting_as(scenario.member, scenario.a) as actor:
        created = service.create_event(
            actor=actor,
            calendar_id=calendar.pk,
            data={"title": "Bloco", "starts_at": moment, "ends_at": moment + timedelta(hours=1), **data},
        )
        if invite:
            service.invite(actor=actor, event_id=created.sysid, user_ids=tuple(invite))
    return created


def respond(scenario, created, user, response):
    from skeleton.contrib.calendar.models import EventAttendee

    with acting_as(user, scenario.a) as actor:
        revision = EventAttendee.objects.get(event_id=created.sysid, membership__user_id=user.pk).revision
        CalendarService().respond(actor=actor, event_id=created.sysid, response=response, expected_revision=revision)


def busy(scenario, users, *, include_attending=False):
    with acting_as(scenario.member, scenario.a) as actor:
        return CalendarService().free_busy(
            actor=actor, user_ids=tuple(users), include_attending=include_attending, **WINDOW
        )


def test_without_the_flag_invitations_do_not_count(scenario):
    calendar = room(scenario)
    event(scenario, calendar, invite=(scenario.admin_a.pk,))
    assert busy(scenario, (scenario.admin_a.pk,)) == ()


def test_attended_events_count_unless_declined(scenario):
    calendar = room(scenario)
    accepted = event(scenario, calendar, hours=0, invite=(scenario.admin_a.pk,))
    respond(scenario, accepted, scenario.admin_a, "accepted")
    tentative = event(scenario, calendar, hours=2, invite=(scenario.admin_a.pk,))
    respond(scenario, tentative, scenario.admin_a, "tentative")
    event(scenario, calendar, hours=4, invite=(scenario.admin_a.pk,))  # needs_action
    declined = event(scenario, calendar, hours=6, invite=(scenario.admin_a.pk,))
    respond(scenario, declined, scenario.admin_a, "declined")
    cancelled = event(scenario, calendar, hours=8, invite=(scenario.admin_a.pk,))
    with acting_as(scenario.member, scenario.a) as actor:
        CalendarService().cancel_event(actor=actor, event_id=cancelled.sysid, expected_revision=cancelled.revision)
    blocks = busy(scenario, (scenario.admin_a.pk,), include_attending=True)
    assert [block.starts_at for block in blocks] == [START, START + timedelta(hours=2), START + timedelta(hours=4)]
    assert all(isinstance(block, BusyBlock) and block.user_id == scenario.admin_a.pk for block in blocks)


def test_recurrence_is_expanded_and_an_excluded_occurrence_does_not_count(scenario):
    calendar = room(scenario)
    series = event(scenario, calendar, invite=(scenario.admin_a.pk,), rrule="FREQ=DAILY;COUNT=3")
    with acting_as(scenario.member, scenario.a) as actor:
        CalendarService().cancel_occurrence(
            actor=actor,
            event_id=series.sysid,
            recurrence_id=START + timedelta(days=1),
            expected_revision=series.revision,
        )
    blocks = busy(scenario, (scenario.admin_a.pk,), include_attending=True)
    assert [block.starts_at for block in blocks] == [START, START + timedelta(days=2)]


def test_the_owner_who_also_attends_counts_once(scenario):
    with acting_as(scenario.member, scenario.a) as actor:
        personal = CalendarService().my_calendars(actor=actor)[0]
    created = event(scenario, Calendar(pk=personal.sysid), invite=(scenario.admin_a.pk,))
    del created
    blocks = busy(scenario, (scenario.member.pk,), include_attending=True)
    assert len(blocks) == 1


def test_people_of_another_organization_never_enter(scenario):
    foreign_calendar = room(scenario, scenario.b)
    with acting_as(scenario.member, scenario.b) as actor:
        CalendarService().create_event(
            actor=actor,
            calendar_id=foreign_calendar.pk,
            data={"title": "B", "starts_at": START, "ends_at": START + timedelta(hours=1)},
        )
    assert busy(scenario, (scenario.admin_b.pk, scenario.outsider.pk), include_attending=True) == ()


def test_queries_do_not_grow_with_people(scenario):
    calendar = room(scenario)
    for hours in range(3):
        event(scenario, calendar, hours=hours, invite=(scenario.admin_a.pk,))

    def count(users):
        with acting_as(scenario.member, scenario.a) as actor, CaptureQueriesContext(connection) as queries:
            CalendarService().free_busy(actor=actor, user_ids=tuple(users), include_attending=True, **WINDOW)
        return len(queries)

    one = count((scenario.admin_a.pk,))
    many = count((scenario.admin_a.pk, scenario.member.pk, *(uuid4() for _ in range(20))))
    assert many == one


def test_calendar_busy_returns_only_times_of_readable_calendars(scenario):
    first, second = room(scenario), room(scenario, name="Sala 2")
    event(scenario, first, hours=0)
    event(scenario, second, hours=1)
    with acting_as(scenario.admin_a, scenario.a) as actor:
        blocks = CalendarService().calendar_busy(actor=actor, calendar_ids=(first.pk, second.pk), **WINDOW)
    assert {(block.calendar_id, block.starts_at) for block in blocks} == {
        (first.pk, START),
        (second.pk, START + timedelta(hours=1)),
    }
    assert all(isinstance(block, CalendarBusyBlock) for block in blocks)
    assert not hasattr(blocks[0], "title")


def test_calendar_busy_refuses_unreadable_and_too_many_calendars(scenario):
    private = room(scenario, visibility="private", owner=scenario.admin_a)
    foreign = room(scenario, scenario.b)
    for calendar_ids in ((private.pk,), (foreign.pk,), (uuid4(),)):
        with acting_as(scenario.member, scenario.a) as actor, pytest.raises(NotFound):
            CalendarService().calendar_busy(actor=actor, calendar_ids=calendar_ids, **WINDOW)
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(InvalidInput):
        CalendarService().calendar_busy(actor=actor, calendar_ids=tuple(uuid4() for _ in range(51)), **WINDOW)


def test_calendar_busy_queries_do_not_grow_with_calendars(scenario):
    calendars = [room(scenario, name=f"Sala {index}") for index in range(6)]
    for index, calendar in enumerate(calendars):
        event(scenario, calendar, hours=index)

    def count(ids):
        with acting_as(scenario.member, scenario.a) as actor, CaptureQueriesContext(connection) as queries:
            CalendarService().calendar_busy(actor=actor, calendar_ids=tuple(ids), **WINDOW)
        return len(queries)

    assert count([calendars[0].pk]) == count([calendar.pk for calendar in calendars])
