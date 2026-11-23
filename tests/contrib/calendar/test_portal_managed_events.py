"""Managed events written from the portal by the app that manages the calendar (#741).

Inside one ``allow_external`` method, the app numbers with its sequence, publishes its event and
puts the managed event in its calendar, in one transaction, with the external person as the actor.
Runs on PostgreSQL with the runtime role.
"""

import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection, transaction

from skeleton.contrib.calendar.models import Calendar, Event, EventAttendee
from skeleton.contrib.calendar.service_api import CalendarService
from skeleton.service_api import Forbidden, InvalidInput, NotFound
from skeleton.tenancy.context import system_context
from skeleton.testing.external import acting_external, assert_external_contract, external_scenario

pytestmark = pytest.mark.needs_example_app
APP = "example_notes"
APP_MODULE = "examples.notes.portal_calendar_fixture"
KEY = "example_notes.portal_appointment"
BOOKED = "notes.portal_appointment_booked"
START = datetime(2027, 6, 7, 13, 0, tzinfo=UTC)

APP_SOURCE = '''
from datetime import timedelta

from skeleton.contrib.calendar.service_api import CalendarService
from skeleton.service_api import NotFound, ServiceAPI, service_method
from skeleton.service_api.events import EventService
from skeleton.service_api.external import ExternalAccessService
from skeleton.service_api.sequences import SequenceService

LINKS = {}


class NotesPortal(ServiceAPI):
    app_label = "example_notes"

    @service_method(read_only=False, since="0.6.0", allow_external=True)
    def book(self, *, actor, note_id, calendar_id, starts_at, source_app="example_notes", attendees=()) -> str:
        """Book a slot for the person linked to the note: number, event and calendar entry.

        Autorização: só no portal, pela ligação da app ao acesso da pessoa.
        Erros: Forbidden fora do portal; NotFound para nota não ligada.
        Efeitos: numera, publica e projeta no calendário na transação do chamador.
        """
        access = ExternalAccessService().me(actor=actor)
        if note_id not in LINKS.get(access.sysid, ()):
            raise NotFound()
        number = SequenceService().next_number(actor=actor, key="example_notes.portal_appointment")
        EventService().publish(
            actor=actor,
            event_type="notes.portal_appointment_booked",
            record_id=note_id,
            data=(("code", number.formatted),),
        )
        CalendarService().put_managed_event(
            actor=actor,
            source_app=source_app,
            source_key=f"booking-{note_id}",
            calendar_id=calendar_id,
            data={"title": "Atendimento", "starts_at": starts_at, "ends_at": starts_at + timedelta(hours=1)},
            attendee_user_ids=attendees,
            source_path="apps/example_notes/bookings/",
        )
        return number.formatted

    @service_method(read_only=False, since="0.6.0", allow_external=True)
    def cancel(self, *, actor, note_id, source_app="example_notes") -> None:
        """Remove the calendar entry of the person's booking.

        Autorização: só no portal, pela ligação da app ao acesso da pessoa.
        Erros: Forbidden fora do portal; NotFound para nota não ligada.
        Efeitos: remove o evento gerenciado.
        """
        access = ExternalAccessService().me(actor=actor)
        if note_id not in LINKS.get(access.sysid, ()):
            raise NotFound()
        CalendarService().remove_managed_event(actor=actor, source_app=source_app, source_key=f"booking-{note_id}")

    @service_method(read_only=True, since="0.6.0", allow_external=True)
    def booked(self, *, actor, note_id):
        """The calendar entry of the person's booking, or None.

        Autorização: só no portal, pela ligação da app ao acesso da pessoa.
        Erros: Forbidden fora do portal; NotFound para nota não ligada.
        Efeitos: nenhum.
        """
        access = ExternalAccessService().me(actor=actor)
        if note_id not in LINKS.get(access.sysid, ()):
            raise NotFound()
        return CalendarService().managed_event(actor=actor, source_app="example_notes", source_key=f"booking-{note_id}")


def direct(function, **kwargs):
    """The app's code calling the calendar outside any of its service methods (a portal view)."""
    return function(**kwargs)
'''


def load_app_module():
    namespace = {"__name__": APP_MODULE}
    exec(APP_SOURCE, namespace)  # noqa: S102 - fixed test source, run as the app's module
    return namespace


APP_CODE = load_app_module()
NotesPortal = APP_CODE["NotesPortal"]
LINKS = APP_CODE["LINKS"]


def calendar(organization, owner, *, source_app=APP, name="Atendimentos"):
    with system_context(reason="test:portal-calendar", organization_id=organization.pk):
        row = Calendar(
            organization=organization,
            owner=owner,
            visibility=Calendar.Visibility.PRIVATE,
            kind=Calendar.Kind.SHARED,
            name=name,
            timezone="America/Sao_Paulo",
            source_app=source_app,
            source_path=f"apps/{source_app}/" if source_app else "",
        )
        row.save()
        return row


@pytest.fixture
def scenario(skeleton_db, monkeypatch):
    from examples.notes.models import Note
    from skeleton.contrib.calendar.services import grant_member_access
    from skeleton.events.registry import EventType, register_event_type
    from skeleton.sequences import registry as sequences
    from skeleton.sequences.registry import SequenceSpec, register_sequence
    from skeleton.tenancy.models import ExternalAccess
    from skeleton.testing.applications import activate_app, open_app
    from skeleton.testing.registries import isolated_event_types
    from skeleton.testing.service_api import acting_as

    monkeypatch.setattr(sequences, "_sequences", dict(sequences._sequences))
    register_sequence(SequenceSpec(KEY, period="none", template="C-{number:04d}", owner_app=APP))
    with isolated_event_types(keep_consumers=True):
        register_event_type(EventType(BOOKED, "example_notes.note", ("code",), owner_app=APP))
        found = external_scenario("portal-calendar")
        for label in ("skeleton_calendar", APP):
            open_app(label)
            for organization in (found.a, found.b):
                activate_app(label, organization.pk)
        with system_context(reason="test:portal-note", organization_id=found.a.pk):
            note = Note(organization=found.a, owner=found.admin_a, code="N-1", title="Pedido")
            note.save()
            access = ExternalAccess.objects.get(user=found.external)
        with acting_as(found.admin_a, found.a) as admin:
            grant_member_access(actor=admin)
        LINKS.clear()
        LINKS[access.pk] = (note.pk,)
        yield found, note, calendar(found.a, found.member)
        LINKS.clear()


def rows(organization):
    from skeleton.events.models import DomainEvent
    from skeleton.sequences.models import SequenceCounter

    with system_context(reason="test:portal-calendar-rows", organization_id=organization.pk):
        return (
            {row.key: row.last_value for row in SequenceCounter.objects.filter(key=KEY)},
            [event.data["code"] for event in DomainEvent.objects.filter(event_type=BOOKED).order_by("occurred_at")],
            list(Event.objects.filter(source_app=APP).values_list("calendar_id", "starts_at", "organizer_id")),
        )


def test_one_method_numbers_publishes_and_puts_the_managed_event(scenario):
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.context import tenant_context

    found, note, agenda = scenario
    with acting_external(found.external, found.a) as actor:
        assert NotesPortal().book(actor=actor, note_id=note.pk, calendar_id=agenda.pk, starts_at=START) == "C-0001"
        entry = NotesPortal().booked(actor=actor, note_id=note.pk)
        assert entry.calendar_id == agenda.pk and entry.title == "Atendimento" and not entry.busy_only
        later = START + timedelta(days=1)
        assert NotesPortal().book(actor=actor, note_id=note.pk, calendar_id=agenda.pk, starts_at=later) == "C-0002"
    counters, published, events = rows(found.a)
    assert counters == {KEY: 2} and published == ["C-0001", "C-0002"]
    assert events == [(agenda.pk, later, found.member.pk)]  # Moved, one live event, organized by the owner.
    with tenant_context(organization_id=found.a.pk, actor=found.admin_a, source="test:portal-calendar-audit"):
        event = Event.all_objects.get(source_app=APP)
        audited = AuditEvent.objects.filter(record_sysid=event.pk)
        assert {(row.actor_sysid, row.source) for row in audited} == {(found.external.pk, "http:portal")}
    with acting_external(found.external, found.a) as actor:
        NotesPortal().cancel(actor=actor, note_id=note.pk)
        assert NotesPortal().booked(actor=actor, note_id=note.pk) is None
        NotesPortal().cancel(actor=actor, note_id=note.pk)  # Idempotent.
    assert rows(found.a)[2] == []


def test_a_rollback_undoes_the_number_the_event_and_the_calendar_entry(scenario):
    found, note, agenda = scenario
    with acting_external(found.external, found.a) as actor:
        with pytest.raises(RuntimeError), transaction.atomic():
            NotesPortal().book(actor=actor, note_id=note.pk, calendar_id=agenda.pk, starts_at=START)
            raise RuntimeError("the booking failed after the calendar")
    assert rows(found.a) == ({}, [], [])
    with acting_external(found.external, found.a) as actor:
        assert NotesPortal().book(actor=actor, note_id=note.pk, calendar_id=agenda.pk, starts_at=START) == "C-0001"


def test_only_a_calendar_the_app_manages_and_only_its_own_events(scenario):
    found, note, agenda = scenario
    unmanaged = calendar(found.a, found.member, source_app="", name="Livre")
    foreign = calendar(found.a, found.member, source_app="other_app", name="Outra")
    with acting_external(found.external, found.a) as actor:
        for other in (unmanaged, foreign):
            with pytest.raises(NotFound):
                NotesPortal().book(actor=actor, note_id=note.pk, calendar_id=other.pk, starts_at=START)
        with pytest.raises(Forbidden) as error:  # Another app's label, whatever it is.
            NotesPortal().book(
                actor=actor, note_id=note.pk, calendar_id=agenda.pk, starts_at=START, source_app="other_app"
            )
        assert error.value.code != "external_not_allowed"
        with pytest.raises(InvalidInput):  # The portal never resolves members as attendees.
            NotesPortal().book(
                actor=actor, note_id=note.pk, calendar_id=agenda.pk, starts_at=START, attendees=(found.member.pk,)
            )
    assert rows(found.a) == ({}, [], [])


def test_an_event_the_app_put_elsewhere_is_not_moved_or_removed_from_the_portal(scenario):
    from skeleton.testing.service_api import acting_as

    found, note, agenda = scenario
    unmanaged = calendar(found.a, found.member, source_app="", name="Livre")
    direct = APP_CODE["direct"]
    with acting_as(found.admin_a, found.a) as admin:
        direct(
            CalendarService().put_managed_event,
            actor=admin,
            source_app=APP,
            source_key=f"booking-{note.pk}",
            calendar_id=unmanaged.pk,
            data={"title": "Atendimento", "starts_at": START, "ends_at": START + timedelta(hours=1)},
            attendee_user_ids=(found.member.pk,),
            source_path="apps/example_notes/bookings/",
        )
    with acting_external(found.external, found.a) as actor:
        with pytest.raises(Forbidden):
            NotesPortal().book(actor=actor, note_id=note.pk, calendar_id=agenda.pk, starts_at=START)
        assert NotesPortal().booked(actor=actor, note_id=note.pk) is None
        NotesPortal().cancel(actor=actor, note_id=note.pk)  # Silent: not an event of its calendar.
    with system_context(reason="test:portal-kept", organization_id=found.a.pk):
        event = Event.objects.get(source_app=APP)
        assert event.calendar_id == unmanaged.pk and EventAttendee.objects.filter(event=event).count() == 1


def test_the_portal_keeps_the_attendees_a_member_path_set(scenario):
    from skeleton.testing.service_api import acting_as

    found, note, agenda = scenario
    direct = APP_CODE["direct"]
    with acting_as(found.admin_a, found.a) as admin:
        direct(
            CalendarService().put_managed_event,
            actor=admin,
            source_app=APP,
            source_key=f"booking-{note.pk}",
            calendar_id=agenda.pk,
            data={"title": "Atendimento", "starts_at": START, "ends_at": START + timedelta(hours=1)},
            attendee_user_ids=(found.member.pk,),
            source_path="apps/example_notes/bookings/",
        )
    with acting_external(found.external, found.a) as actor:
        NotesPortal().book(actor=actor, note_id=note.pk, calendar_id=agenda.pk, starts_at=START + timedelta(hours=2))
    with system_context(reason="test:portal-attendees", organization_id=found.a.pk):
        event = Event.objects.get(source_app=APP)
        assert event.starts_at == START + timedelta(hours=2)
        assert list(EventAttendee.objects.filter(event=event).values_list("membership__user_id", flat=True)) == [
            found.member.pk
        ]


def test_outside_an_allow_external_method_the_calendar_stays_closed(scenario):
    found, note, agenda = scenario
    direct = APP_CODE["direct"]
    data = {"title": "Atendimento", "starts_at": START, "ends_at": START + timedelta(hours=1)}
    with acting_external(found.external, found.a) as actor:
        for call in (
            lambda: direct(
                CalendarService().put_managed_event,
                actor=actor,
                source_app=APP,
                source_key="direct",
                calendar_id=agenda.pk,
                data=data,
            ),
            lambda: direct(CalendarService().remove_managed_event, actor=actor, source_app=APP, source_key="direct"),
            lambda: direct(CalendarService().managed_event, actor=actor, source_app=APP, source_key="direct"),
            lambda: CalendarService().calendars(actor=actor),
        ):
            with pytest.raises(Forbidden) as error:
                call()
            assert error.value.code == "external_not_allowed"
    assert rows(found.a)[2] == []


def test_concurrent_bookings_of_the_same_entry_leave_one_event(scenario):
    found, note, agenda = scenario
    barrier = threading.Barrier(2)
    results = []

    def run(starts_at):
        try:
            barrier.wait()
            with acting_external(found.external, found.a) as actor:
                results.append(
                    NotesPortal().book(actor=actor, note_id=note.pk, calendar_id=agenda.pk, starts_at=starts_at)
                )
        except Exception as error:  # noqa: BLE001
            results.append(type(error).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(START + timedelta(hours=hour),)) for hour in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["C-0001", "C-0002"], results
    counters, published, events = rows(found.a)
    assert counters == {KEY: 2} and sorted(published) == ["C-0001", "C-0002"] and len(events) == 1


def test_the_kit_proves_the_portal_contract_of_the_booking(scenario):
    from skeleton.system.applications import application_state, configure_application
    from skeleton.testing.service_api import acting_as

    found, note, agenda = scenario

    @contextmanager
    def disabled():
        def configure(enabled):
            with acting_as(found.admin_a, found.a):
                state = application_state(APP)
                configure_application(
                    APP,
                    user=found.admin_a,
                    enabled=enabled,
                    configuration=state["configuration"],
                    revision=state["revision"],
                )

        configure(False)
        try:
            yield
        finally:
            configure(True)

    def call(actor):
        try:
            return NotesPortal().book(actor=actor, note_id=note.pk, calendar_id=agenda.pk, starts_at=START)
        except NotFound:
            return None

    assert_external_contract(
        NotesPortal, "book", scenario=found, call=call, unlinked=lambda result: result is None, disabled=disabled
    )
