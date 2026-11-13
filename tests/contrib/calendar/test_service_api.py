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


# --------------------------------------------------------------------------- bounded windows (#291)


def _events(scenario, calendar, count, *, start=START):
    from skeleton.testing.service_api import acting_as

    with acting_as(scenario.member, scenario.a) as actor:
        for index in range(count):
            moment = start + timedelta(hours=index)
            CalendarService().create_event(
                actor=actor,
                calendar_id=calendar,
                data=event_data(title=f"E{index}", starts_at=moment, ends_at=moment + timedelta(minutes=30)),
            )


def _upcoming_queries(scenario, user):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from skeleton.testing.service_api import acting_as

    with acting_as(user, scenario.a) as actor, CaptureQueriesContext(connection) as queries:
        result = CalendarService().upcoming(actor=actor, start=START - timedelta(days=1), end=START + timedelta(days=8))
    return len(queries), result


def test_upcoming_query_count_does_not_grow_with_events(scenario):
    calendar = shared(scenario).pk
    _events(scenario, calendar, 10)
    few, result = _upcoming_queries(scenario, scenario.member)
    assert len(result) == 10
    _events(scenario, calendar, 90, start=START + timedelta(days=3))
    many, result = _upcoming_queries(scenario, scenario.member)
    assert len(result) == 100 and many == few


def test_upcoming_in_someone_elses_personal_calendar_has_constant_queries(scenario):
    from skeleton.testing.service_api import acting_as

    with acting_as(scenario.member, scenario.a) as actor:
        personal = CalendarService().my_calendars(actor=actor)[0].sysid
    _events(scenario, personal, 10)
    few, result = _upcoming_queries(scenario, scenario.admin_a)
    assert len(result) == 10 and all(item.busy_only for item in result)
    _events(scenario, personal, 90, start=START + timedelta(days=3))
    many, result = _upcoming_queries(scenario, scenario.admin_a)
    assert len(result) == 100 and many == few


def test_free_busy_query_count_is_constant(scenario):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from skeleton.testing.service_api import acting_as

    with acting_as(scenario.member, scenario.a) as actor:
        personal = CalendarService().my_calendars(actor=actor)[0].sysid
    _events(scenario, personal, 3)

    def busy(user_ids):
        with acting_as(scenario.admin_a, scenario.a) as actor, CaptureQueriesContext(connection) as queries:
            blocks = CalendarService().free_busy(
                actor=actor, user_ids=user_ids, start=START - timedelta(days=1), end=START + timedelta(days=1)
            )
        return len(queries), blocks

    one, blocks = busy((scenario.member.pk,))
    assert [block.starts_at for block in blocks] == [START + timedelta(hours=index) for index in range(3)]
    ten, again = busy((scenario.member.pk, *(uuid4() for _ in range(9))))
    assert ten == one and again == blocks


def test_window_over_400_days_is_invalid_without_events(scenario):
    from skeleton.service_api import InvalidInput
    from skeleton.testing.service_api import acting_as

    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(InvalidInput):
        CalendarService().upcoming(actor=actor, start=START, end=START + timedelta(days=401))


def test_too_many_occurrences_is_invalid_input(scenario, monkeypatch):
    from skeleton.contrib.calendar import service_api
    from skeleton.service_api import InvalidInput
    from skeleton.testing.service_api import acting_as

    _events(scenario, shared(scenario).pk, 4)
    monkeypatch.setattr(service_api, "MAX_OCCURRENCES_PER_CALL", 3)
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(InvalidInput):
        CalendarService().upcoming(actor=actor, start=START - timedelta(days=1), end=START + timedelta(days=1))


def test_actor_view_does_not_leak_between_actors(scenario):
    """A delegate sees the content; the next caller, without delegation, sees a busy block."""
    from skeleton.access.models import RecordGrant
    from skeleton.service_api.access import AccessService
    from skeleton.system.models import TableDefinition
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Membership
    from skeleton.testing.service_api import acting_as

    with acting_as(scenario.member, scenario.a) as actor:
        personal = CalendarService().my_calendars(actor=actor)[0].sysid
    _events(scenario, personal, 1)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        AccessService().grant_member_table_access(
            actor=admin, table_key="skeleton_calendar.calendar", actions=("read",)
        )
    with system_context(reason="test:calendar-delegation", organization_id=scenario.a.pk):
        table = TableDefinition.objects.get(model_label="skeleton_calendar.calendar")
        RecordGrant(
            organization=scenario.a,
            table=table,
            record_sysid=personal,
            membership=Membership.objects.get(user=scenario.denied),
            action="read",
        ).save()
    _, delegated = _upcoming_queries(scenario, scenario.denied)
    assert [item.busy_only for item in delegated] == [False]
    _, other = _upcoming_queries(scenario, scenario.admin_a)  # Administrator, but no delegation.
    assert [item.busy_only for item in other] == [True]


def test_all_day_occurrences_carry_dates(scenario):
    from skeleton.testing.service_api import acting_as

    calendar = shared(scenario).pk
    day = START.date()
    with acting_as(scenario.member, scenario.a) as actor:
        CalendarService().create_event(
            actor=actor,
            calendar_id=calendar,
            data={"title": "Dia", "all_day": True, "start_date": day, "end_date": day + timedelta(days=2)},
        )
        CalendarService().create_event(actor=actor, calendar_id=calendar, data=event_data(title="Hora"))
        rows = {
            item.title: item
            for item in CalendarService().upcoming(
                actor=actor, start=START - timedelta(days=1), end=START + timedelta(days=3)
            )
        }
    assert (rows["Dia"].start_date, rows["Dia"].end_date) == (day, day + timedelta(days=2))
    assert (rows["Hora"].start_date, rows["Hora"].end_date) == (None, None)
    assert_service_contract(CalendarService)
