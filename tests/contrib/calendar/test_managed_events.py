"""Managed events (#537): events another app projects into the calendar, read-only outside its service."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from skeleton.contrib.calendar.models import Calendar, Event, EventAttendee
from skeleton.contrib.calendar.service_api import CalendarService
from skeleton.service_api import Conflict, Forbidden, InvalidInput, NotFound
from skeleton.tenancy.context import system_context
from skeleton.testing.applications import activate_app, open_app
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

pytestmark = pytest.mark.needs_example_app
SOURCE = "example_notes"
START = datetime(2027, 5, 3, 13, 0, tzinfo=UTC)
DATA = {"title": "Atendimento", "starts_at": START, "ends_at": START + timedelta(hours=1)}


@pytest.fixture
def scenario(skeleton_db):
    scenario = build_service_scenario(Calendar, slug="calendar-managed")
    open_app(SOURCE)
    for organization in (scenario.a, scenario.b):
        activate_app(SOURCE, organization.pk)
    return scenario


def shared(scenario, organization=None, owner=None):
    return scenario.create(
        organization or scenario.a, owner=owner or scenario.member, name="Atendimentos", kind="shared"
    )


def put(scenario, calendar, *, key="appt-1", data=None, attendees=(), path="", user=None, organization=None):
    return scenario.caller(
        lambda actor: CalendarService().put_managed_event(
            actor=actor,
            source_app=SOURCE,
            source_key=key,
            calendar_id=calendar.pk,
            data=dict(DATA if data is None else data),
            attendee_user_ids=attendees,
            source_path=path,
        ),
        user=user,
        organization=organization,
    )


def notifications(organization):
    from skeleton.notifications.models import Notification

    with system_context(reason="test:count-notifications", organization_id=organization.pk):
        return Notification.objects.count()


def test_the_service_keeps_its_contract():
    assert_service_contract(CalendarService)


def test_put_creates_once_and_updates_only_on_change(scenario):
    calendar = shared(scenario)
    first = put(scenario, calendar, path=f"apps/{SOURCE}/appointments/1/")()
    assert first.source_app == SOURCE and first.source_path == f"apps/{SOURCE}/appointments/1/"
    again = put(scenario, calendar, path=f"apps/{SOURCE}/appointments/1/")()
    assert again.sysid == first.sysid and again.revision == first.revision
    moved = put(scenario, calendar, path=f"apps/{SOURCE}/appointments/1/", data={**DATA, "title": "Retorno"})()
    assert moved.sysid == first.sysid and moved.revision == first.revision + 1
    with acting_as(scenario.member, scenario.a):
        row = Event.objects.get(pk=first.sysid)
        assert row.sequence == 1 and row.is_private and row.rrule == "" and row.organizer_id == scenario.member.pk


def test_a_repeated_key_never_creates_two_events(scenario):
    calendar = shared(scenario)
    put(scenario, calendar)()
    put(scenario, calendar)()
    with acting_as(scenario.member, scenario.a):
        assert Event.objects.filter(source_app=SOURCE, source_key="appt-1").count() == 1


def test_another_calendar_replaces_the_event(scenario):
    first_calendar, second_calendar = shared(scenario), shared(scenario)
    first = put(scenario, first_calendar)()
    second = put(scenario, second_calendar)()
    assert second.sysid != first.sysid and second.calendar_id == second_calendar.pk
    with acting_as(scenario.member, scenario.a):
        assert not Event.objects.filter(pk=first.sysid).exists()


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"data": {**DATA, "description": "detalhes do cliente"}}, "description"),
        ({"data": {**DATA, "rrule": "FREQ=DAILY"}}, "__all__"),
        ({"path": "apps/other/x/"}, "source_path"),
        ({"path": f"apps/{SOURCE}/../secret/"}, "source_path"),
        ({"path": f"/apps/{SOURCE}/x/"}, "source_path"),
        ({"path": f"https://apps/{SOURCE}/x/"}, "source_path"),
        ({"key": "bad key!"}, "source_key"),
    ],
)
def test_invalid_input_is_refused(scenario, changes, field):
    calendar = shared(scenario)
    with pytest.raises(InvalidInput) as error:
        put(scenario, calendar, **changes)()
    assert field in error.value.errors


@pytest.mark.parametrize("source", ["skeleton_calendar", "unknown_app", "Bad-Label"])
def test_the_source_app_must_be_another_active_app(scenario, source):
    calendar = shared(scenario)
    call = scenario.caller(
        lambda actor: CalendarService().put_managed_event(
            actor=actor, source_app=source, source_key="k", calendar_id=calendar.pk, data=dict(DATA)
        )
    )
    with pytest.raises(InvalidInput) as error:
        call()
    assert "source_app" in error.value.errors


def test_an_inactive_source_app_is_refused(scenario):
    from skeleton.testing.service_api import _set_enabled

    calendar = shared(scenario)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        _set_enabled(SOURCE, admin, enabled=False)
    with pytest.raises(InvalidInput):
        put(scenario, calendar)()


def test_attendees_are_synchronized_accepted_and_never_notified(scenario):
    calendar = shared(scenario)
    before = notifications(scenario.a)
    event = put(scenario, calendar, attendees=(scenario.admin_a.pk, scenario.member.pk))()
    put(scenario, calendar, attendees=(scenario.admin_a.pk,))()
    with acting_as(scenario.member, scenario.a):
        rows = list(EventAttendee.objects.filter(event_id=event.sysid).select_related("membership"))
    assert [(row.membership.user_id, row.response) for row in rows] == [(scenario.admin_a.pk, "accepted")]
    with pytest.raises(InvalidInput):
        put(scenario, calendar, attendees=(scenario.outsider.pk,))()
    scenario.caller(
        lambda actor: CalendarService().remove_managed_event(actor=actor, source_app=SOURCE, source_key="appt-1")
    )()
    assert notifications(scenario.a) == before


def test_authorization_of_the_calendar(scenario):
    readable = scenario.create(
        scenario.a, owner=scenario.admin_a, name="Da equipe", kind="shared", visibility="organization"
    )
    with pytest.raises(Forbidden):
        put(scenario, readable)()
    private = scenario.create(scenario.a, owner=scenario.admin_a, name="Privado", kind="shared", visibility="private")
    with pytest.raises(NotFound):
        put(scenario, private)()
    foreign = shared(scenario, scenario.b)
    with pytest.raises(NotFound):
        put(scenario, foreign)()
    assert_rejects_other_actor(
        scenario.caller(
            lambda actor: CalendarService().managed_event(actor=scenario.admin_a, source_app=SOURCE, source_key="x")
        )
    )


def test_the_system_context_projects_with_the_calendar_owner_as_organizer(scenario):
    calendar = shared(scenario)
    with system_context(reason="test:managed-job", organization_id=scenario.a.pk):
        info = CalendarService().put_managed_event(
            actor=None, source_app=SOURCE, source_key="from-job", calendar_id=calendar.pk, data=dict(DATA)
        )
        assert info.organizer_id == scenario.member.pk and info.title == "Atendimento"
        assert CalendarService().managed_event(actor=None, source_app=SOURCE, source_key="from-job").sysid == info.sysid
        CalendarService().remove_managed_event(actor=None, source_app=SOURCE, source_key="from-job")
        assert CalendarService().managed_event(actor=None, source_app=SOURCE, source_key="from-job") is None


def test_general_methods_refuse_managed_events_and_keep_ordinary_ones(scenario):
    calendar = shared(scenario)
    managed = put(scenario, calendar)()
    service = CalendarService()
    calls = {
        "update_event": lambda a: service.update_event(
            actor=a, event_id=managed.sysid, data={"title": "x"}, expected_revision=managed.revision
        ),
        "cancel_event": lambda a: service.cancel_event(
            actor=a, event_id=managed.sysid, expected_revision=managed.revision
        ),
        "delete_event": lambda a: service.delete_event(
            actor=a, event_id=managed.sysid, expected_revision=managed.revision
        ),
        "edit_occurrence": lambda a: service.edit_occurrence(
            actor=a,
            event_id=managed.sysid,
            recurrence_id=START,
            mode="this",
            data={},
            expected_revision=managed.revision,
        ),
        "cancel_occurrence": lambda a: service.cancel_occurrence(
            actor=a, event_id=managed.sysid, recurrence_id=START, expected_revision=managed.revision
        ),
        "invite": lambda a: service.invite(actor=a, event_id=managed.sysid, user_ids=(scenario.admin_a.pk,)),
        "add_reminder": lambda a: service.add_reminder(actor=a, event_id=managed.sysid, minutes_before=10),
    }
    for name, call in calls.items():
        with pytest.raises(Conflict) as error:
            scenario.caller(call)()
        assert error.value.code == "managed_event", name
    put(scenario, calendar, attendees=(scenario.admin_a.pk,))()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        with pytest.raises(Conflict) as error:
            service.respond(actor=admin, event_id=managed.sysid, response="declined", expected_revision=1)
        assert error.value.code == "managed_event"
    ordinary = scenario.caller(lambda a: service.create_event(actor=a, calendar_id=calendar.pk, data=dict(DATA)))()
    changed = scenario.caller(
        lambda a: service.update_event(
            actor=a, event_id=ordinary.sysid, data={"title": "y"}, expected_revision=ordinary.revision
        )
    )()
    assert changed.title == "y" and changed.source_app == ""


def test_remove_is_idempotent(scenario):
    calendar = shared(scenario)
    put(scenario, calendar)()
    for _ in range(2):
        scenario.caller(
            lambda actor: CalendarService().remove_managed_event(actor=actor, source_app=SOURCE, source_key="appt-1")
        )()
    assert (
        scenario.caller(
            lambda actor: CalendarService().managed_event(actor=actor, source_app=SOURCE, source_key="appt-1")
        )()
        is None
    )


def test_private_by_default_and_masked_in_ics(scenario):
    from skeleton.contrib.calendar.ics import calendar_bytes, vevent

    calendar = scenario.create(
        scenario.a, owner=scenario.member, name="Visível", kind="shared", visibility="organization"
    )
    event = put(scenario, calendar, path=f"apps/{SOURCE}/appointments/9/")()
    seen = scenario.caller(
        lambda actor: CalendarService().event(actor=actor, event_id=event.sysid), user=scenario.admin_a
    )()
    assert seen.busy_only and seen.title == "Ocupado" and seen.source_app == "" and seen.source_path == ""
    with acting_as(scenario.admin_a, scenario.a) as admin:
        text = calendar_bytes("Agenda", [vevent(admin, Event.objects.select_related("calendar").get(pk=event.sysid))])
    assert b"CLASS:PRIVATE" in text and b"Atendimento" not in text and b"appointments" not in text


def test_isolation_and_disabled_apps(scenario):
    foreign_calendar = shared(scenario, scenario.b)
    foreign = put(scenario, foreign_calendar, key="b-appt", organization=scenario.b)()
    assert_tenant_isolated(
        scenario.caller(
            lambda actor: CalendarService().managed_event(actor=actor, source_app=SOURCE, source_key="b-appt") or ()
        ),
        foreign=[foreign.sysid],
    )
    calendar = shared(scenario)
    assert_unavailable_when_disabled(
        put(scenario, calendar, key=f"k-{uuid4().hex[:8]}"), disabled=scenario.application_disabled
    )


def test_the_audit_redacts_the_title(scenario):
    from skeleton.audit.models import AuditEvent

    calendar = shared(scenario)
    event = put(scenario, calendar)()
    put(scenario, calendar, data={**DATA, "title": "Outro"})()
    scenario.caller(
        lambda actor: CalendarService().remove_managed_event(actor=actor, source_app=SOURCE, source_key="appt-1")
    )()
    with acting_as(scenario.admin_a, scenario.a):
        events = list(AuditEvent.objects.filter(record_sysid=event.sysid).order_by("created_at"))
    assert [row.operation for row in events] == ["create", "update", "soft_delete"]
    assert events[0].changes["title"]["new"] == "[REDACTED]"


def test_the_loser_of_a_race_for_the_same_key_reads_the_winner(scenario, monkeypatch):
    from skeleton.contrib.calendar import services

    calendar = shared(scenario)
    winner = put(scenario, calendar)()
    real = services.managed_event_row
    calls = []

    def stale_first_lookup(source_app, source_key, *, lock=False):
        calls.append(lock)
        return None if len(calls) == 1 else real(source_app, source_key, lock=lock)

    monkeypatch.setattr(services, "managed_event_row", stale_first_lookup)
    loser = put(scenario, calendar, data={**DATA, "title": "Atualizado"})()
    assert loser.sysid == winner.sysid and loser.revision == winner.revision + 1 and len(calls) == 2
    with acting_as(scenario.member, scenario.a):
        assert Event.objects.filter(source_app=SOURCE, source_key="appt-1").count() == 1
