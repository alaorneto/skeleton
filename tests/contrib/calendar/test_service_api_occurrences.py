"""CalendarService event and occurrence operations (#344): event, delete, edit and cancel an occurrence."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from skeleton.contrib.calendar.models import Calendar
from skeleton.contrib.calendar.service_api import CalendarService, EventInfo
from skeleton.service_api import Forbidden, InvalidInput
from skeleton.testing.service_api import (
    acting_as,
    assert_cas_conflict,
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

START = datetime(2027, 4, 5, 13, 0, tzinfo=UTC)


@pytest.fixture
def scenario(skeleton_db):
    return build_service_scenario(Calendar, slug="calendar-occurrences")


def series(scenario, organization=None, **values):
    organization = organization or scenario.a
    calendar = scenario.create(organization, owner=scenario.member, name="Equipe", kind="shared", **values)
    with acting_as(scenario.member, organization) as actor:
        return CalendarService().create_event(
            actor=actor,
            calendar_id=calendar.pk,
            data={
                "title": "Diária",
                "starts_at": START,
                "ends_at": START + timedelta(hours=1),
                "rrule": "FREQ=DAILY;COUNT=5",
            },
        )


def test_event_delete_edit_and_cancel_occurrence_authorized_path(scenario):
    event = series(scenario)
    service = CalendarService()
    second, third = START + timedelta(days=1), START + timedelta(days=2)
    with acting_as(scenario.member, scenario.a) as actor:
        assert isinstance(service.event(actor=actor, event_id=event.sysid), EventInfo)
        moved = service.edit_occurrence(
            actor=actor,
            event_id=event.sysid,
            recurrence_id=second,
            mode="this",
            data={"title": "Movida"},
            expected_revision=event.revision,
        )
        assert moved.title == "Movida" and moved.sysid != event.sysid
        current = service.event(actor=actor, event_id=event.sysid)
        cancelled = service.cancel_occurrence(
            actor=actor, event_id=event.sysid, recurrence_id=third, expected_revision=current.revision
        )
        assert cancelled.sysid == event.sysid
        with pytest.raises(InvalidInput):
            service.cancel_occurrence(
                actor=actor,
                event_id=event.sysid,
                recurrence_id=third.replace(tzinfo=None),
                expected_revision=cancelled.revision,
            )
        service.delete_event(actor=actor, event_id=event.sysid, expected_revision=cancelled.revision)
    assert_denied_as_not_found(
        scenario.caller(lambda actor: service.event(actor=actor, event_id=event.sysid)),
        missing=scenario.caller(lambda actor: service.event(actor=actor, event_id=uuid4())),
    )


def test_reader_without_write_is_forbidden(scenario):
    """Organization visibility lets the denied person read nothing; a read-only role reads but never writes."""
    from skeleton.service_api.access import AccessService

    event = series(scenario, visibility="organization")
    with acting_as(scenario.admin_a, scenario.a) as admin:
        AccessService().grant_member_table_access(
            actor=admin, table_key="skeleton_calendar.calendar", actions=("read",)
        )
    with acting_as(scenario.denied, scenario.a) as reader:
        CalendarService().event(actor=reader, event_id=event.sysid)  # Readable now.
        for call in (
            lambda: CalendarService().delete_event(
                actor=reader, event_id=event.sysid, expected_revision=event.revision
            ),
            lambda: CalendarService().cancel_occurrence(
                actor=reader, event_id=event.sysid, recurrence_id=START, expected_revision=event.revision
            ),
            lambda: CalendarService().edit_occurrence(
                actor=reader,
                event_id=event.sysid,
                recurrence_id=START,
                mode="this",
                data={"title": "x"},
                expected_revision=event.revision,
            ),
        ):
            with pytest.raises(Forbidden):
                call()


def test_foreign_and_missing_event_are_not_found(scenario):
    foreign = series(scenario, scenario.b)
    for call in (
        lambda actor, target: CalendarService().event(actor=actor, event_id=target),
        lambda actor, target: CalendarService().delete_event(actor=actor, event_id=target, expected_revision=1),
        lambda actor, target: CalendarService().cancel_occurrence(
            actor=actor, event_id=target, recurrence_id=START, expected_revision=1
        ),
    ):
        assert_denied_as_not_found(
            scenario.caller(lambda actor, call=call: call(actor, foreign.sysid)),
            missing=scenario.caller(lambda actor, call=call: call(actor, uuid4())),
        )


def test_occurrence_commands_cas(scenario):
    event = series(scenario)

    def cancel(day):
        return scenario.caller(
            lambda actor: CalendarService().cancel_occurrence(
                actor=actor,
                event_id=event.sysid,
                recurrence_id=START + timedelta(days=day),
                expected_revision=event.revision,
            )
        )

    assert_cas_conflict(cancel(1), cancel(2))


def test_occurrence_commands_unavailable_and_other_actor(scenario):
    event = series(scenario)
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: CalendarService().event(actor=actor, event_id=event.sysid)),
        disabled=scenario.application_disabled,
    )
    assert_rejects_other_actor(
        scenario.caller(
            lambda actor: CalendarService().delete_event(
                actor=scenario.admin_a, event_id=event.sysid, expected_revision=event.revision
            )
        )
    )
