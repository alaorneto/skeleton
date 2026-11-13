"""CalendarService reminder operations (#344): authorized path, NotFound parity, deny, A/B, app off, actor, CAS."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from skeleton.contrib.calendar.models import Calendar
from skeleton.contrib.calendar.service_api import CalendarService, ReminderInfo
from skeleton.service_api import Forbidden, InvalidInput
from skeleton.testing.service_api import (
    acting_as,
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

START = datetime(2027, 4, 5, 13, 0, tzinfo=UTC)


@pytest.fixture
def scenario(skeleton_db):
    return build_service_scenario(Calendar, slug="calendar-reminders")


def meeting(scenario, organization=None):
    organization = organization or scenario.a
    calendar = scenario.create(organization, owner=scenario.member, name="Equipe", kind="shared")
    with acting_as(scenario.member, organization) as actor:
        return CalendarService().create_event(
            actor=actor,
            calendar_id=calendar.pk,
            data={"title": "Reunião", "starts_at": START, "ends_at": START + timedelta(hours=1)},
        )


def test_add_list_remove_authorized_path(scenario):
    event = meeting(scenario)
    service = CalendarService()
    with acting_as(scenario.member, scenario.a) as actor:
        reminder = service.add_reminder(actor=actor, event_id=event.sysid, minutes_before=30)
        assert isinstance(reminder, ReminderInfo) and reminder.minutes_before == 30
        assert service.add_reminder(actor=actor, event_id=event.sysid, minutes_before=30).sysid == reminder.sysid
        assert [row.sysid for row in service.reminders(actor=actor, event_id=event.sysid)] == [reminder.sysid]
        with pytest.raises(InvalidInput):
            service.add_reminder(actor=actor, event_id=event.sysid, minutes_before=40321)
        service.remove_reminder(actor=actor, reminder_id=reminder.sysid, expected_revision=reminder.revision)
        assert service.reminders(actor=actor, event_id=event.sysid) == ()


def test_missing_and_unreadable_reminder_answer_the_same(scenario):
    foreign = meeting(scenario, scenario.b)
    with acting_as(scenario.member, scenario.b) as actor:
        hidden = CalendarService().add_reminder(actor=actor, event_id=foreign.sysid, minutes_before=10)
    assert_denied_as_not_found(
        scenario.caller(
            lambda actor: CalendarService().remove_reminder(actor=actor, reminder_id=hidden.sysid, expected_revision=1)
        ),
        missing=scenario.caller(
            lambda actor: CalendarService().remove_reminder(actor=actor, reminder_id=uuid4(), expected_revision=1)
        ),
    )


def test_reminder_commands_default_deny_isolation_disabled_other_actor(scenario):
    event = meeting(scenario)
    foreign = meeting(scenario, scenario.b)
    with acting_as(scenario.denied, scenario.a) as denied:
        with pytest.raises(Forbidden):
            CalendarService().add_reminder(actor=denied, event_id=event.sysid, minutes_before=5)
        with pytest.raises(Forbidden):
            CalendarService().reminders(actor=denied, event_id=event.sysid)
    assert_denied_as_not_found(
        scenario.caller(lambda actor: CalendarService().reminders(actor=actor, event_id=foreign.sysid)),
        missing=scenario.caller(lambda actor: CalendarService().reminders(actor=actor, event_id=uuid4())),
    )
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: CalendarService().reminders(actor=actor, event_id=event.sysid)),
        disabled=scenario.application_disabled,
    )
    assert_rejects_other_actor(
        scenario.caller(
            lambda actor: CalendarService().add_reminder(actor=scenario.admin_a, event_id=event.sysid, minutes_before=5)
        )
    )


def test_remove_reminder_cas(scenario):
    from skeleton.service_api import Conflict

    event = meeting(scenario)
    with acting_as(scenario.member, scenario.a) as actor:
        reminder = CalendarService().add_reminder(actor=actor, event_id=event.sysid, minutes_before=15)
        with pytest.raises(Conflict) as error:
            CalendarService().remove_reminder(
                actor=actor, reminder_id=reminder.sysid, expected_revision=reminder.revision + 1
            )
        assert error.value.code == "stale_revision"
        CalendarService().remove_reminder(actor=actor, reminder_id=reminder.sysid, expected_revision=reminder.revision)
