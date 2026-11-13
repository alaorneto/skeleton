"""The service contract of AGENTS.md, proven for each original CalendarService method (#320).

Default deny, A/B, app disabled, another actor and CAS, with the kit of ``skeleton.testing``.
The operations published later (#344) have their own files.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from skeleton.contrib.calendar.models import Calendar
from skeleton.contrib.calendar.service_api import CalendarService
from skeleton.service_api import Forbidden
from skeleton.testing.service_api import (
    acting_as,
    assert_cas_conflict,
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

START = datetime(2027, 6, 7, 13, 0, tzinfo=UTC)
FOREIGN_TITLE = "Segredo de B 2027"


@pytest.fixture
def scenario(skeleton_db):
    return build_service_scenario(Calendar, slug="calendar-checklist")


def data(**changes):
    return {"title": "Revisão", "starts_at": START, "ends_at": START + timedelta(hours=1), **changes}


def shared(scenario, organization=None):
    return scenario.create(organization or scenario.a, owner=scenario.member, name="Equipe", kind="shared")


def event_in(scenario, organization=None, **changes):
    organization = organization or scenario.a
    calendar = shared(scenario, organization)
    with acting_as(scenario.member, organization) as actor:
        return CalendarService().create_event(actor=actor, calendar_id=calendar.pk, data=data(**changes))


WINDOW = {"start": START - timedelta(days=1), "end": START + timedelta(days=1)}


def calls(scenario, event, calendar, *, actor_of=None):
    """The six original methods, as functions of the calling actor."""
    service = CalendarService()

    def who(actor):
        return actor_of or actor

    return {
        "my_calendars": lambda actor: service.my_calendars(actor=who(actor)),
        "upcoming": lambda actor: service.upcoming(actor=who(actor), **WINDOW),
        "create_event": lambda actor: service.create_event(actor=who(actor), calendar_id=calendar, data=data()),
        "update_event": lambda actor: service.update_event(
            actor=who(actor), event_id=event.sysid, data={"title": "Nova"}, expected_revision=event.revision
        ),
        "cancel_event": lambda actor: service.cancel_event(
            actor=who(actor), event_id=event.sysid, expected_revision=event.revision
        ),
        "free_busy": lambda actor: service.free_busy(actor=who(actor), user_ids=(scenario.member.pk,), **WINDOW),
    }


METHODS = ("my_calendars", "upcoming", "create_event", "update_event", "cancel_event", "free_busy")


def test_upcoming_and_free_busy_are_tenant_isolated(scenario):
    foreign = event_in(scenario, scenario.b, title=FOREIGN_TITLE)
    event_in(scenario)
    assert_tenant_isolated(
        scenario.caller(lambda actor: CalendarService().upcoming(actor=actor, **WINDOW)),
        foreign=[foreign.sysid, FOREIGN_TITLE],
    )
    with acting_as(scenario.member, scenario.a) as actor:
        assert CalendarService().free_busy(actor=actor, user_ids=(scenario.admin_b.pk,), **WINDOW) == ()


def test_update_and_cancel_of_a_foreign_event_are_not_found(scenario):
    foreign = event_in(scenario, scenario.b)
    service = CalendarService()
    assert_denied_as_not_found(
        scenario.caller(
            lambda actor: service.update_event(
                actor=actor, event_id=foreign.sysid, data={"title": "x"}, expected_revision=foreign.revision
            )
        ),
        missing=scenario.caller(
            lambda actor: service.update_event(actor=actor, event_id=uuid4(), data={"title": "x"}, expected_revision=1)
        ),
    )
    assert_denied_as_not_found(
        scenario.caller(
            lambda actor: service.cancel_event(actor=actor, event_id=foreign.sysid, expected_revision=foreign.revision)
        ),
        missing=scenario.caller(lambda actor: service.cancel_event(actor=actor, event_id=uuid4(), expected_revision=1)),
    )


@pytest.mark.parametrize("method", METHODS)
def test_every_method_is_unavailable_when_the_app_is_disabled(scenario, method):
    event = event_in(scenario)
    calendar = shared(scenario).pk
    call = calls(scenario, event, calendar)[method]
    # The kit's control call runs first with the app enabled; then, disabled, the call is Unavailable
    # before any other check (a stale revision of a repeated command never gets that far).
    assert_unavailable_when_disabled(scenario.caller(call), disabled=scenario.application_disabled)


@pytest.mark.parametrize("method", METHODS)
def test_every_method_rejects_another_actor(scenario, method):
    event = event_in(scenario)
    calendar = shared(scenario).pk
    assert_rejects_other_actor(scenario.caller(calls(scenario, event, calendar, actor_of=scenario.admin_a)[method]))


@pytest.mark.parametrize("method", METHODS)
def test_every_method_denies_by_default(scenario, method):
    event = event_in(scenario)
    calendar = shared(scenario).pk
    call = calls(scenario, event, calendar)[method]
    with pytest.raises(Forbidden):
        scenario.caller(call, user=scenario.denied)()


def test_cancel_event_cas_has_one_winner(scenario):
    event = event_in(scenario)

    def cancel():
        return scenario.caller(
            lambda actor: CalendarService().cancel_event(
                actor=actor, event_id=event.sysid, expected_revision=event.revision
            )
        )

    cancelled = assert_cas_conflict(cancel(), cancel())
    assert cancelled.status == "cancelled"


def test_upcoming_masks_a_personal_calendar_for_the_administrator(scenario):
    from skeleton.contrib.calendar.services import grant_member_access

    with acting_as(scenario.member, scenario.a) as actor:
        personal = CalendarService().my_calendars(actor=actor)[0].sysid
        CalendarService().create_event(actor=actor, calendar_id=personal, data=data(title="Consulta médica"))
    with acting_as(scenario.admin_a, scenario.a) as admin:
        grant_member_access(actor=admin)
        [item] = CalendarService().upcoming(actor=admin, **WINDOW)
    assert item.busy_only is True and item.title == "Ocupado"
