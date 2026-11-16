"""Service calendars with writing delegated to groups (#539), and the core record grant service."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from skeleton.access import services as access
from skeleton.contrib.calendar.models import Calendar
from skeleton.contrib.calendar.service_api import CalendarService
from skeleton.service_api import Forbidden, InvalidInput, NotFound
from skeleton.service_api.access import AccessService
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

START = datetime(2027, 7, 5, 13, 0, tzinfo=UTC)
TABLE = "skeleton_calendar.calendar"


@pytest.fixture
def scenario(skeleton_db):
    return build_service_scenario(Calendar, slug="calendar-shared")


def team(scenario, *, members=()):
    from skeleton.tenancy.models import Membership

    with acting_as(scenario.admin_a, scenario.a) as admin:
        group = access.create_group(user=admin, data={"code": f"team-{uuid4().hex[:6]}", "name": "Apoio"})
        for user in members:
            membership = Membership.objects.get(user=user)
            access.add_group_member(user=admin, data={"group": group, "membership": membership})
    return group


def create(scenario, *, writers=(), readers=(), key="k1", owner=None, user=None):
    return scenario.caller(
        lambda actor: CalendarService().create_shared_calendar(
            actor=actor,
            name="Atendimentos",
            owner_user_id=(owner or scenario.member).pk,
            timezone="America/Sao_Paulo",
            writer_group_ids=tuple(group.pk for group in writers),
            reader_group_ids=tuple(group.pk for group in readers),
            idempotency_key=key,
        ),
        user=user or scenario.admin_a,
    )


def add_event(scenario, calendar_id, user):
    return scenario.caller(
        lambda actor: CalendarService().create_event(
            actor=actor,
            calendar_id=calendar_id,
            data={"title": "Atendimento", "starts_at": START, "ends_at": START + timedelta(hours=1)},
        ),
        user=user,
    )


def test_the_services_keep_their_contract():
    assert_service_contract(CalendarService)
    assert_service_contract(AccessService)


def test_a_group_writes_the_service_calendar_and_outsiders_do_not_read_it(scenario):
    writers = team(scenario, members=(scenario.denied,))
    calendar = create(scenario, writers=(writers,))()
    assert calendar.kind == "shared" and calendar.owner_id == scenario.member.pk
    assert create(scenario, writers=(writers,))().sysid == calendar.sysid  # Same key, same calendar.
    with acting_as(scenario.admin_a, scenario.a):
        row = Calendar.objects.get(pk=calendar.sysid)
        assert row.visibility == "private"
    # The group member has no table permission of their own: the grant alone is not enough (default deny).
    with pytest.raises((NotFound, Forbidden)):
        add_event(scenario, calendar.sysid, scenario.denied)()
    member_writers = team(scenario, members=(scenario.member,))
    other = create(scenario, writers=(member_writers,), key="k2", owner=scenario.admin_a)()
    assert add_event(scenario, other.sysid, scenario.member)().calendar_id == other.sysid
    owned = add_event(scenario, calendar.sysid, scenario.member)()
    assert owned.calendar_id == calendar.sysid  # The owner reads and writes.


def test_a_reader_group_reads_without_writing(scenario):
    readers = team(scenario, members=(scenario.member,))
    calendar = create(scenario, readers=(readers,), owner=scenario.admin_a)()
    listed = scenario.caller(lambda actor: CalendarService().calendars(actor=actor))()
    assert calendar.sysid in {item.sysid for item in listed}
    assert not next(item for item in listed if item.sysid == calendar.sysid).writable
    with pytest.raises(Forbidden):
        add_event(scenario, calendar.sysid, scenario.member)()


def test_only_who_manages_access_creates_service_calendars(scenario):
    with pytest.raises(Forbidden):
        create(scenario, user=scenario.member)()
    with pytest.raises(InvalidInput):
        create(scenario, owner=scenario.outsider)()


def test_the_administrator_still_does_not_write_a_personal_calendar_of_someone_else(scenario):
    with acting_as(scenario.member, scenario.a) as actor:
        personal = CalendarService().my_calendars(actor=actor)[0]
    with pytest.raises(Forbidden):
        add_event(scenario, personal.sysid, scenario.admin_a)()


def test_grant_and_revoke_record_access_follow_the_access_screens(scenario):
    writers = team(scenario, members=(scenario.member,))
    calendar = scenario.create(
        scenario.a, owner=scenario.admin_a, name="Sala", kind="shared", visibility="organization"
    )
    service = AccessService()

    def grant(user=None, **subject):
        return scenario.caller(
            lambda actor: service.grant_record_access(
                actor=actor, table_key=TABLE, record_id=calendar.pk, action="update", **subject
            ),
            user=user or scenario.admin_a,
        )

    first = grant(group_id=writers.pk)()
    assert grant(group_id=writers.pk)().sysid == first.sysid  # Idempotent.
    assert first.group_id == writers.pk and first.action == "update"
    with pytest.raises(Forbidden):
        grant(user=scenario.member, group_id=writers.pk)()
    with pytest.raises(InvalidInput):
        grant()()
    with pytest.raises(InvalidInput):
        grant(group_id=writers.pk, role_id=uuid4())()
    foreign_group = None
    with acting_as(scenario.admin_b, scenario.b) as admin:
        foreign_group = access.create_group(user=admin, data={"code": "b-team", "name": "B"})
    with pytest.raises(InvalidInput):
        grant(group_id=foreign_group.pk)()
    with pytest.raises(NotFound):
        scenario.caller(
            lambda actor: service.grant_record_access(
                actor=actor, table_key=TABLE, record_id=uuid4(), action="update", group_id=writers.pk
            ),
            user=scenario.admin_a,
        )()
    scenario.caller(
        lambda actor: service.revoke_record_access(
            actor=actor, table_key=TABLE, record_id=calendar.pk, action="update", group_id=writers.pk
        ),
        user=scenario.admin_a,
    )()
    from skeleton.access.models import RecordGrant

    with acting_as(scenario.admin_a, scenario.a):
        assert not RecordGrant.objects.filter(pk=first.sysid).exists()


def test_isolation_disabled_app_and_actor(scenario):
    foreign = scenario.create(scenario.b, owner=scenario.admin_b, name="B", kind="shared")
    writers = team(scenario)
    with pytest.raises(NotFound):
        scenario.caller(
            lambda actor: AccessService().grant_record_access(
                actor=actor, table_key=TABLE, record_id=foreign.pk, action="read", group_id=writers.pk
            ),
            user=scenario.admin_a,
        )()
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: CalendarService().calendars(actor=actor)), disabled=scenario.application_disabled
    )
    assert_rejects_other_actor(scenario.caller(lambda actor: CalendarService().calendars(actor=scenario.admin_a)))
