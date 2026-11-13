"""CalendarService through the contract kit (#111): masking, NotFound, Unavailable, CAS, idempotency."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from skeleton.contrib.calendar.models import Calendar
from skeleton.contrib.calendar.service_api import BusyBlock, CalendarService, EventInfo
from skeleton.service_api import Forbidden
from skeleton.testing.service_api import (
    assert_cas_conflict,
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

START = datetime(2026, 12, 10, 13, 0, tzinfo=UTC)


@pytest.fixture
def scenario(skeleton_db):
    return build_service_scenario(Calendar, slug="calendar-service")


def shared(scenario, organization=None, **values):
    return scenario.create(organization or scenario.a, owner=scenario.member, name="Equipe", kind="shared", **values)


def event_data(**changes):
    return {"title": "Revisão", "starts_at": START, "ends_at": START + timedelta(hours=1), **changes}


def test_calendar_service_passes_the_static_contract():
    assert_service_contract(CalendarService)


def test_authorized_path_and_idempotency(scenario):
    calendar = shared(scenario)
    with scenario.as_member() as actor:
        mine = CalendarService().my_calendars(actor=actor)
        assert mine[0].kind == "personal" and mine[0].is_default
        created = CalendarService().create_event(
            actor=actor, calendar_id=calendar.pk, data=event_data(), idempotency_key="sync-1"
        )
        assert isinstance(created, EventInfo) and created.title == "Revisão" and not created.busy_only
        again = CalendarService().create_event(
            actor=actor, calendar_id=calendar.pk, data=event_data(title="Outro"), idempotency_key="sync-1"
        )
        assert again.sysid == created.sysid and again.title == "Revisão"
        [occurrence] = CalendarService().upcoming(
            actor=actor, start=START - timedelta(days=1), end=START + timedelta(days=1)
        )
        assert occurrence.event_id == created.sysid
        changed = CalendarService().update_event(
            actor=actor, event_id=created.sysid, data={"title": "Nova"}, expected_revision=created.revision
        )
        assert changed.title == "Nova"
        assert (
            CalendarService()
            .cancel_event(actor=actor, event_id=created.sysid, expected_revision=changed.revision)
            .status
            == "cancelled"
        )


def test_private_events_are_busy_for_readers_who_are_not_organizer_or_owner(scenario):
    calendar = shared(scenario, visibility="organization")
    with scenario.as_member() as actor:
        private = CalendarService().create_event(actor=actor, calendar_id=calendar.pk, data=event_data(is_private=True))
    from skeleton.contrib.calendar.services import grant_member_access

    with scenario.acting(scenario.admin_a) as admin:
        grant_member_access(actor=admin)
        [seen] = CalendarService().upcoming(
            actor=admin, start=START - timedelta(hours=1), end=START + timedelta(hours=2)
        )
    assert seen.event_id == private.sysid and seen.title == "Ocupado" and seen.busy_only


def test_foreign_calendar_is_not_found_and_tenants_are_isolated(scenario):
    foreign = shared(scenario, organization=scenario.b, description="segredo de B")
    assert_denied_as_not_found(
        scenario.caller(
            lambda actor: CalendarService().create_event(actor=actor, calendar_id=foreign.pk, data=event_data())
        ),
        missing=scenario.caller(
            lambda actor: CalendarService().create_event(actor=actor, calendar_id=uuid4(), data=event_data())
        ),
    )
    assert_tenant_isolated(
        scenario.caller(lambda actor: CalendarService().my_calendars(actor=actor)), foreign=[foreign, "segredo de B"]
    )


def test_default_deny_read_only_and_other_actor(scenario):
    calendar = shared(scenario, visibility="organization")
    with pytest.raises(Forbidden):
        scenario.caller(lambda actor: CalendarService().my_calendars(actor=actor), user=scenario.denied)()
    assert_rejects_other_actor(scenario.caller(lambda actor: CalendarService().my_calendars(actor=scenario.admin_a)))
    # A reader without update on the calendar cannot create in it.
    from skeleton.testing.service_api import acting_as

    with acting_as(scenario.admin_a, scenario.a) as admin:
        from skeleton.service_api.access import AccessService

        AccessService().grant_member_table_access(
            actor=admin, table_key="skeleton_calendar.calendar", actions=("read",)
        )
    with pytest.raises(Forbidden):
        scenario.caller(
            lambda actor: CalendarService().create_event(actor=actor, calendar_id=calendar.pk, data=event_data()),
            user=scenario.denied,
        )()


def test_disabled_app_is_unavailable(scenario):
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: CalendarService().my_calendars(actor=actor)),
        disabled=scenario.application_disabled,
    )


def test_cas_has_one_winner(scenario):
    calendar = shared(scenario)
    with scenario.as_member() as actor:
        created = CalendarService().create_event(actor=actor, calendar_id=calendar.pk, data=event_data())
    assert_cas_conflict(
        scenario.caller(
            lambda actor: CalendarService().update_event(
                actor=actor, event_id=created.sysid, data={"title": "A"}, expected_revision=created.revision
            )
        ),
        scenario.caller(
            lambda actor: CalendarService().update_event(
                actor=actor, event_id=created.sysid, data={"title": "B"}, expected_revision=created.revision
            )
        ),
    )


def test_free_busy_returns_only_times(scenario):
    with scenario.as_member() as actor:
        personal = CalendarService().my_calendars(actor=actor)[0]
        CalendarService().create_event(actor=actor, calendar_id=personal.sysid, data=event_data(title="Médico"))
    with scenario.acting(scenario.admin_a) as admin:
        from skeleton.contrib.calendar.services import grant_member_access

        grant_member_access(actor=admin)
        blocks = CalendarService().free_busy(
            actor=admin,
            user_ids=(scenario.member.pk,),
            start=START - timedelta(hours=1),
            end=START + timedelta(hours=3),
        )
    assert blocks == (BusyBlock(scenario.member.pk, START, START + timedelta(hours=1)),)
    assert "Médico" not in repr(blocks)
