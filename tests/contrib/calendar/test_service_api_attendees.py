"""CalendarService attendee operations (#344): authorized path, default deny, A/B, app off, actor and CAS."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from skeleton.contrib.calendar.models import Calendar
from skeleton.contrib.calendar.service_api import AttendeeInfo, CalendarService
from skeleton.service_api import Forbidden, InvalidInput, NotFound
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
    return build_service_scenario(Calendar, slug="calendar-attendees")


def meeting(scenario, organization=None):
    organization = organization or scenario.a
    calendar = scenario.create(organization, owner=scenario.member, name="Equipe", kind="shared")
    with acting_as(scenario.member, organization) as actor:
        return CalendarService().create_event(
            actor=actor,
            calendar_id=calendar.pk,
            data={"title": "Reunião", "starts_at": START, "ends_at": START + timedelta(hours=1)},
        )


def test_invite_list_respond_and_remove_authorized_path(scenario):
    event = meeting(scenario)
    service = CalendarService()
    with acting_as(scenario.member, scenario.a) as actor:
        [invitation] = service.invite(actor=actor, event_id=event.sysid, user_ids=(scenario.admin_a.pk,))
        assert isinstance(invitation, AttendeeInfo) and invitation.user_id == scenario.admin_a.pk
        assert service.invite(actor=actor, event_id=event.sysid, user_ids=(scenario.admin_a.pk,)) == ()
        assert [row.sysid for row in service.attendees(actor=actor, event_id=event.sysid)] == [invitation.sysid]
    with acting_as(scenario.admin_a, scenario.a) as admin:
        answered = service.respond(
            actor=admin, event_id=event.sysid, response="accepted", expected_revision=invitation.revision
        )
        assert answered.response == "accepted" and answered.revision > invitation.revision
    with acting_as(scenario.member, scenario.a) as actor:
        service.remove_attendee(actor=actor, attendee_id=invitation.sysid, expected_revision=answered.revision)
        assert service.attendees(actor=actor, event_id=event.sysid) == ()


def test_invite_refuses_outsiders_and_service_accounts(scenario):
    from skeleton.accounts.services import create_service_account

    event = meeting(scenario)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        robot = create_service_account(actor=admin, name="Robô")
    with acting_as(scenario.member, scenario.a) as actor:
        for user in (scenario.outsider, robot, scenario.admin_b):
            with pytest.raises(InvalidInput):
                CalendarService().invite(actor=actor, event_id=event.sysid, user_ids=(user.pk,))
        with pytest.raises(InvalidInput):
            CalendarService().invite(actor=actor, event_id=event.sysid, user_ids=tuple(uuid4() for _ in range(101)))


def test_respond_only_for_oneself_is_not_found(scenario):
    event = meeting(scenario)
    with acting_as(scenario.member, scenario.a) as actor:
        CalendarService().invite(actor=actor, event_id=event.sysid, user_ids=(scenario.admin_a.pk,))
    # The organizer has no invitation of their own: someone else's does not exist for them.
    assert_denied_as_not_found(
        scenario.caller(
            lambda actor: CalendarService().respond(
                actor=actor, event_id=event.sysid, response="declined", expected_revision=1
            )
        ),
        missing=scenario.caller(
            lambda actor: CalendarService().respond(
                actor=actor, event_id=uuid4(), response="declined", expected_revision=1
            )
        ),
    )


def test_attendee_commands_default_deny_and_tenant_isolation(scenario):
    event = meeting(scenario)
    foreign = meeting(scenario, scenario.b)
    with acting_as(scenario.member, scenario.a) as actor:
        [invitation] = CalendarService().invite(actor=actor, event_id=event.sysid, user_ids=(scenario.admin_a.pk,))
    with acting_as(scenario.denied, scenario.a) as denied:
        for call in (
            lambda: CalendarService().attendees(actor=denied, event_id=event.sysid),
            lambda: CalendarService().invite(actor=denied, event_id=event.sysid, user_ids=(scenario.member.pk,)),
            lambda: CalendarService().remove_attendee(
                actor=denied, attendee_id=invitation.sysid, expected_revision=invitation.revision
            ),
        ):
            with pytest.raises(Forbidden):
                call()
    for call in (
        lambda actor: CalendarService().attendees(actor=actor, event_id=foreign.sysid),
        lambda actor: CalendarService().invite(actor=actor, event_id=foreign.sysid, user_ids=(scenario.member.pk,)),
    ):
        assert_denied_as_not_found(
            scenario.caller(call),
            missing=scenario.caller(lambda actor: CalendarService().attendees(actor=actor, event_id=uuid4())),
        )
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(NotFound):
        CalendarService().remove_attendee(actor=actor, attendee_id=uuid4(), expected_revision=1)


def test_attendee_commands_unavailable_when_disabled(scenario):
    event = meeting(scenario)
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: CalendarService().attendees(actor=actor, event_id=event.sysid)),
        disabled=scenario.application_disabled,
    )


def test_attendee_commands_reject_other_actor(scenario):
    event = meeting(scenario)
    assert_rejects_other_actor(
        scenario.caller(lambda actor: CalendarService().attendees(actor=scenario.admin_a, event_id=event.sysid))
    )
    assert_rejects_other_actor(
        scenario.caller(
            lambda actor: CalendarService().invite(
                actor=scenario.admin_a, event_id=event.sysid, user_ids=(scenario.member.pk,)
            )
        )
    )


def test_respond_and_remove_cas(scenario):
    event = meeting(scenario)
    with acting_as(scenario.member, scenario.a) as actor:
        [invitation] = CalendarService().invite(actor=actor, event_id=event.sysid, user_ids=(scenario.admin_a.pk,))

    def respond(answer):
        return scenario.caller(
            lambda actor: CalendarService().respond(
                actor=actor, event_id=event.sysid, response=answer, expected_revision=invitation.revision
            ),
            user=scenario.admin_a,
        )

    answered = assert_cas_conflict(respond("accepted"), respond("declined"))

    def remove(revision):
        return scenario.caller(
            lambda actor: CalendarService().remove_attendee(
                actor=actor, attendee_id=invitation.sysid, expected_revision=revision
            )
        )

    from skeleton.service_api import Conflict

    with pytest.raises(Conflict):
        remove(invitation.revision)()  # The RSVP moved the revision.
    remove(answered.revision)()
