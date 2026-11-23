"""Sequences and events of the calling app in the portal (#741).

An app's ``allow_external`` method numbers with its own sequence and publishes its own event type,
with the external person as the actor, in the request's transaction; nothing else opens. Runs on
PostgreSQL with the runtime role.
"""

import threading
from contextlib import contextmanager

import pytest
from django.db import connection, transaction

from skeleton.service_api import Forbidden, NotFound
from skeleton.service_api.events import EventService
from skeleton.service_api.sequences import SequenceService
from skeleton.tenancy.context import system_context, tenant_context
from skeleton.testing.external import acting_external, assert_external_contract, external_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
APP = "example_notes"
APP_MODULE = "examples.notes.portal_fixture"
KEY = "example_notes.portal_ticket"
FOREIGN_KEY = "example_notes.foreign_ticket"
BOOKED = "notes.portal_booked"
FOREIGN_EVENT = "notes.foreign_booked"

# The app's own code: defined in a module of the app, as ``calling_app_label`` requires.
APP_SOURCE = '''
from skeleton.service_api import ServiceAPI, service_method
from skeleton.service_api.events import EventService
from skeleton.service_api.external import ExternalAccessService
from skeleton.service_api.sequences import SequenceService

LINKS = {}


class NotesPortal(ServiceAPI):
    app_label = "example_notes"

    @service_method(read_only=False, since="0.6.0", allow_external=True)
    def book(self, *, actor, note_id, key="example_notes.portal_ticket", event_type="notes.portal_booked") -> str:
        """Number a booking and publish it, for the person linked to the note.

        Autorização: só no portal, pela ligação da app ao acesso da pessoa.
        Erros: Forbidden fora do portal; NotFound para nota não ligada.
        Efeitos: numera e publica na transação do chamador.
        """
        from skeleton.service_api import NotFound

        access = ExternalAccessService().me(actor=actor)
        if note_id not in LINKS.get(access.sysid, ()):
            raise NotFound()
        number = SequenceService().next_number(actor=actor, key=key)
        EventService().publish(actor=actor, event_type=event_type, record_id=note_id, data=(("code", number.formatted),))
        return number.formatted


def direct(function, **kwargs):
    """The app's code calling a core surface outside any of its service methods (a portal view)."""
    return function(**kwargs)
'''


def load_app_module():
    namespace = {"__name__": APP_MODULE}
    exec(APP_SOURCE, namespace)  # noqa: S102 - fixed test source, run as the app's module
    return namespace


APP_CODE = load_app_module()
NotesPortal = APP_CODE["NotesPortal"]
LINKS = APP_CODE["LINKS"]


@pytest.fixture
def scenario(monkeypatch):
    from examples.notes.models import Note
    from skeleton.events.registry import EventType, register_event_type
    from skeleton.sequences import registry as sequences
    from skeleton.sequences.registry import SequenceSpec, register_sequence
    from skeleton.tenancy.models import ExternalAccess
    from skeleton.testing.applications import activate_app, open_app
    from skeleton.testing.registries import isolated_event_types

    monkeypatch.setattr(sequences, "_sequences", dict(sequences._sequences))
    register_sequence(SequenceSpec(KEY, period="none", template="P-{number:04d}", owner_app=APP))
    register_sequence(SequenceSpec(FOREIGN_KEY, period="none", owner_app="other_app"))
    with isolated_event_types(keep_consumers=True):
        register_event_type(EventType(BOOKED, "example_notes.note", ("code",), owner_app=APP))
        register_event_type(EventType(FOREIGN_EVENT, "example_notes.note", ("code",), owner_app="other_app"))
        found = external_scenario("portal-owner")
        open_app(APP)
        for organization in (found.a, found.b):
            activate_app(APP, organization.pk)
        with system_context(reason="test:portal-note", organization_id=found.a.pk):
            note = Note(organization=found.a, owner=found.admin_a, code="N-1", title="Pedido")
            note.save()
            access = ExternalAccess.objects.get(user=found.external)
        LINKS.clear()
        LINKS[access.pk] = (note.pk,)
        yield found, note
        LINKS.clear()


def counters(organization):
    from skeleton.sequences.models import SequenceCounter

    with system_context(reason="test:portal-counters", organization_id=organization.pk):
        return {row.key: row.last_value for row in SequenceCounter.objects.all()}


def events(organization):
    from skeleton.events.models import DomainEvent

    with system_context(reason="test:portal-events", organization_id=organization.pk):
        return list(DomainEvent.objects.filter(event_type__in=(BOOKED, FOREIGN_EVENT)).order_by("occurred_at"))


def test_an_allow_external_method_numbers_and_publishes_as_the_person(scenario):
    from skeleton.audit.models import AuditEvent
    from skeleton.sequences.models import SequenceCounter

    found, note = scenario
    with acting_external(found.external, found.a) as actor:
        assert NotesPortal().book(actor=actor, note_id=note.pk) == "P-0001"
        assert NotesPortal().book(actor=actor, note_id=note.pk) == "P-0002"
    [first, second] = events(found.a)
    assert (first.actor_id, first.source, first.data) == (found.external.pk, "http:portal", {"code": "P-0001"})
    assert second.record_sysid == note.pk and second.data == {"code": "P-0002"}
    with tenant_context(organization_id=found.a.pk, actor=found.admin_a, source="test:portal-audit"):
        counter = SequenceCounter.objects.get(key=KEY)
        audited = AuditEvent.objects.filter(record_sysid=counter.pk).order_by("created_at")
        assert {(row.actor_sysid, row.source) for row in audited} == {(found.external.pk, "http:portal")}


def test_a_rollback_undoes_the_number_and_the_event(scenario):
    found, note = scenario
    with acting_external(found.external, found.a) as actor:
        with pytest.raises(RuntimeError), transaction.atomic():
            NotesPortal().book(actor=actor, note_id=note.pk)
            raise RuntimeError("the booking failed after numbering")
        assert NotesPortal().book(actor=actor, note_id=note.pk) == "P-0001"  # The number came back.
    assert [event.data["code"] for event in events(found.a)] == ["P-0001"]


def test_another_apps_sequence_or_event_type_is_forbidden(scenario):
    found, note = scenario
    with acting_external(found.external, found.a) as actor:
        for kwargs in ({"key": FOREIGN_KEY}, {"event_type": FOREIGN_EVENT}):
            with pytest.raises(Forbidden) as error:
                NotesPortal().book(actor=actor, note_id=note.pk, **kwargs)
            assert error.value.code != "external_not_allowed"
    assert counters(found.a).get(FOREIGN_KEY) is None and events(found.a) == []


def test_outside_an_allow_external_method_nothing_opens(scenario):
    found, note = scenario
    direct = APP_CODE["direct"]
    with acting_external(found.external, found.a) as actor:
        for call in (
            lambda: direct(SequenceService().next_number, actor=actor, key=KEY),
            lambda: direct(
                EventService().publish, actor=actor, event_type=BOOKED, record_id=note.pk, data=(("code", "x"),)
            ),
            lambda: SequenceService().next_number(actor=actor, key=KEY),
        ):
            with pytest.raises(Forbidden) as error:
                call()
            assert error.value.code == "external_not_allowed"
    with tenant_context(organization_id=found.a.pk, actor=found.member, source="test:portal-member"):
        with pytest.raises(Forbidden) as error:
            NotesPortal().book(actor=found.member, note_id=note.pk)  # A member is not in the portal.
        assert error.value.code == "external_only"
    assert counters(found.a) == {} and events(found.a) == []


def test_the_person_of_another_organization_reaches_nothing_of_a(scenario):
    from skeleton.tenancy.models import ExternalAccess
    from skeleton.testing.external import grant_external_access

    found, note = scenario
    grant_external_access(found.b, found.outsider)
    with system_context(reason="test:portal-b-link", organization_id=found.b.pk):
        LINKS[ExternalAccess.objects.get(user=found.outsider).pk] = (note.pk,)  # A forged link to A's note.
    with acting_external(found.outsider, found.b) as actor, pytest.raises(NotFound):
        NotesPortal().book(actor=actor, note_id=note.pk)
    assert events(found.a) == [] and events(found.b) == [] and counters(found.b) == {}


def test_concurrent_bookings_get_consecutive_numbers(scenario):
    found, note = scenario
    barrier = threading.Barrier(2)
    results = []

    def run():
        try:
            barrier.wait()
            with acting_external(found.external, found.a) as actor:
                results.append(NotesPortal().book(actor=actor, note_id=note.pk))
        except Exception as error:  # noqa: BLE001
            results.append(type(error).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["P-0001", "P-0002"], results
    assert sorted(event.data["code"] for event in events(found.a)) == ["P-0001", "P-0002"]


def test_the_kit_proves_the_portal_contract_of_the_booking(scenario):
    from skeleton.system.applications import application_state, configure_application
    from skeleton.testing.service_api import acting_as

    found, note = scenario

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
            return NotesPortal().book(actor=actor, note_id=note.pk, key=KEY)
        except NotFound:
            return None

    assert_external_contract(
        NotesPortal,
        "book",
        scenario=found,
        call=call,
        unlinked=lambda result: result is None,
        disabled=disabled,
    )
