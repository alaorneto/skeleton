"""JobService and EventService of the core service API (#108)."""

import pytest

from skeleton.events.core_events import CORE_EVENT_TYPES
from skeleton.events.models import DomainEvent
from skeleton.events.registry import EventType, register_event_type
from skeleton.jobs.models import Job
from skeleton.jobs.registry import JobType, register_job_type
from skeleton.service_api.errors import Forbidden, InvalidInput, NotFound
from skeleton.service_api.events import EventService
from skeleton.service_api.jobs import JobService
from skeleton.tenancy.context import system_context
from skeleton.testing.registries import isolated_event_types, isolated_job_types
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
NOTES_MODULE = "examples.notes.services"


def as_module(module, function):
    namespace = {"__name__": module, "function": function}
    exec("def call(*args, **kwargs):\n    return function(*args, **kwargs)\n", namespace)  # noqa: S102
    return namespace["call"]


def handler(**kwargs):
    return None


def test_contracts():
    assert_service_contract(JobService)
    assert_service_contract(EventService)


@pytest.fixture
def scenario(monkeypatch):

    from examples.notes.models import Note

    # Copies keep the core job and event types (#43); nothing registered here outlives the test.
    with isolated_job_types(keep_existing=True), isolated_event_types(keep_consumers=True):
        register_job_type(JobType("notes.reindex", handler, {"note_id": "uuid"}, owner_app="example_notes"))
        register_job_type(JobType("other.work", handler, {}, owner_app="other_app"))
        register_event_type(EventType("notes.closed", "example_notes.note", ("code",), owner_app="example_notes"))
        register_event_type(EventType("other.happened", "example_notes.note", (), owner_app="other_app"))
        yield build_service_scenario(Note, slug="jobs-events")


def enqueue_as_notes(**kwargs):
    return as_module(NOTES_MODULE, JobService().enqueue)(**kwargs)


def publish_as_notes(**kwargs):
    return as_module(NOTES_MODULE, EventService().publish)(**kwargs)


@pytest.mark.needs_example_app
def test_enqueue_own_type_with_receipt_and_idempotency(scenario):
    with acting_as(scenario.member, scenario.a) as actor:
        payload = (("note_id", str(actor.pk)),)
        first = enqueue_as_notes(actor=actor, job_type="notes.reindex", payload=payload, idempotency_key="k")
        again = enqueue_as_notes(actor=actor, job_type="notes.reindex", payload=payload, idempotency_key="k")
    assert first.sysid == again.sysid and first.created and not again.created
    assert first.state == "queued" and not hasattr(first, "payload")
    assert Job.all_objects.get(pk=first.sysid).job_organization_id == scenario.a.pk


@pytest.mark.needs_example_app
def test_foreign_type_invalid_payload_and_rollback(scenario):
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(Forbidden):
            enqueue_as_notes(actor=actor, job_type="other.work")
        with pytest.raises(Forbidden):  # The caller is not the owning app.
            JobService().enqueue(actor=actor, job_type="notes.reindex", payload=(("note_id", str(actor.pk)),))
        with pytest.raises(InvalidInput):
            enqueue_as_notes(actor=actor, job_type="notes.reindex", payload=(("note_id", "x"),))
        with pytest.raises(InvalidInput):
            enqueue_as_notes(actor=actor, job_type="notes.reindex", payload=(("extra", 1),))
        with pytest.raises(InvalidInput):
            enqueue_as_notes(actor=actor, job_type="unknown")
    with pytest.raises(RuntimeError), acting_as(scenario.member, scenario.a) as actor:
        enqueue_as_notes(actor=actor, job_type="notes.reindex", payload=(("note_id", str(actor.pk)),))
        raise RuntimeError("caller failure")
    assert not Job.all_objects.filter(job_type="notes.reindex").exists()
    assert_rejects_other_actor(
        scenario.caller(lambda actor: enqueue_as_notes(actor=scenario.denied, job_type="notes.reindex"))
    )


@pytest.mark.needs_example_app
def test_publish_own_event_about_a_readable_record(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="EV-1", title="T")
    with acting_as(scenario.member, scenario.a) as actor:
        receipt = publish_as_notes(actor=actor, event_type="notes.closed", record_id=note.pk, data=(("code", "EV-1"),))
    assert receipt.record_id == note.pk
    with system_context(reason="test:events-read", organization_id=scenario.a.pk):
        assert DomainEvent.objects.get(pk=receipt.sysid).data == {"code": "EV-1"}
        publish_as_notes(actor=None, event_type="notes.closed", record_id=note.pk)


@pytest.mark.needs_example_app
def test_publish_refuses_foreign_type_record_and_rolls_back(scenario):
    hidden = scenario.create(scenario.a, owner=scenario.admin_a, code="EV-2", title="T", visibility="private")
    foreign = scenario.create(scenario.b, owner=scenario.member, code="EV-3", title="T")
    own = scenario.create(scenario.a, owner=scenario.member, code="EV-4", title="T")
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(Forbidden):
            publish_as_notes(actor=actor, event_type="other.happened", record_id=hidden.pk)
        for record in (hidden, foreign):
            with pytest.raises(NotFound):
                publish_as_notes(actor=actor, event_type="notes.closed", record_id=record.pk)
        with pytest.raises(InvalidInput):
            publish_as_notes(actor=actor, event_type="notes.closed", record_id=own.pk, data=(("body", "x"),))
    with pytest.raises(RuntimeError), acting_as(scenario.member, scenario.a) as actor:
        publish_as_notes(actor=actor, event_type="notes.closed", record_id=own.pk)
        raise RuntimeError("caller failure")
    with system_context(reason="test:events-none", organization_id=scenario.a.pk):
        assert not DomainEvent.objects.exclude(event_type__in=CORE_EVENT_TYPES).exists()
