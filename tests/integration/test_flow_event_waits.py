"""A wait advanced by a domain event of the app, with automatic correlation (#411)."""

from types import MappingProxyType

import pytest

from examples.notes.models import Note
from skeleton.flows import registry as flows
from skeleton.flows.event_waits import deliver
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_event_types
from skeleton.testing.service_api import build_service_scenario
from tests.integration.test_flow_service_api import approval_spec, as_app, service

pytestmark = pytest.mark.needs_example_app
EVENT = "example_notes.note_checked"
KEY = "example_notes.event_wait"


def event_spec(**wait):
    return flows.FlowSpec(
        key=KEY,
        version=1,
        label="Espera por evento",
        owner_app="example_notes",
        subject_table="example_notes.note",
        steps=(
            flows.start("start"),
            flows.wait("checked", **({"event": EVENT} | wait)),
            flows.end("done", outcome="checked"),
        ),
        transitions=(flows.Transition("start", "checked"), flows.Transition("checked", "done", on="received")),
    )


@pytest.fixture
def world(skeleton_db, monkeypatch):
    from skeleton.events.registry import EventType, register_event_type
    from skeleton.flows import registry

    with isolated_event_types(keep_consumers=True):
        register_event_type(EventType(EVENT, table_key="example_notes.note", owner_app="example_notes"))
        register_event_type(
            EventType("example_notes.other_event", table_key="example_notes.note", owner_app="example_notes")
        )
        for name in ("_flows", "_handlers", "_conditions", "_assignments"):
            monkeypatch.setattr(registry, name, {})
        monkeypatch.setattr(registry, "_duplicates", [])
        flows.register_flow(event_spec())
        assert flows.flow_problems() == []
        built = build_service_scenario(Note, slug="event-wait")
        built.note = built.create(built.a, code="w1", title="Espera")
        built.other = built.create(built.a, code="w2", title="Outra")
        built.foreign = built.create(built.b, code="w3", title="De B")
        yield built


def begin(world, note, organization=None):
    return world.caller(
        lambda actor: as_app(service.start)(actor=actor, flow_key=KEY, subject_id=note.pk), organization=organization
    )()


def status(world, receipt, organization=None):
    return world.caller(
        lambda actor: service.instance(actor=actor, instance_id=receipt.sysid), organization=organization
    )().status


def publish(world, code, note, organization=None):
    from skeleton.events.services import publish_event

    with world.acting(world.admin_a if organization is None else world.admin_b, organization or world.a):
        publish_event(code, record=Note.objects.get(pk=note.pk))


def test_the_correlated_event_advances_the_wait_and_others_do_not(world):
    mine = begin(world, world.note)
    other = begin(world, world.other)
    theirs = begin(world, world.foreign, organization=world.b)
    publish(world, "example_notes.other_event", world.note)  # Another type: nothing waits for it.
    publish(world, EVENT, world.note)
    run_pending_jobs()
    assert status(world, mine) == "completed"
    assert status(world, other) == "running"  # Another record of the same flow.
    assert status(world, theirs, organization=world.b) == "running"  # The event was in A.
    publish(world, EVENT, world.foreign, organization=world.b)
    run_pending_jobs()
    assert status(world, theirs, organization=world.b) == "completed"


def test_a_repeated_delivery_changes_nothing(world):
    from skeleton.flows.models import FlowActivity
    from skeleton.tenancy.context import system_context

    receipt = begin(world, world.note)
    publish(world, EVENT, world.note)
    run_pending_jobs()
    envelope = type("Envelope", (), {})()
    envelope.event_type, envelope.table_key, envelope.record_sysid = EVENT, "example_notes.note", world.note.pk
    envelope.data = MappingProxyType({})
    with system_context(reason="test:event-wait-repeat", organization_id=world.a.pk):
        before = FlowActivity.objects.filter(instance_id=receipt.sysid).count()
        deliver(envelope)
        assert FlowActivity.objects.filter(instance_id=receipt.sysid).count() == before


def test_the_check_refuses_unknown_foreign_or_ambiguous_event_waits(world):
    from skeleton.events.registry import EventType, register_event_type

    register_event_type(EventType("example_other.thing", table_key="example_notes.note", owner_app="example_other"))
    assert any("not registered" in item for item in flows.spec_problems(event_spec(event="example_notes.missing")))
    assert any("the app's own" in item for item in flows.spec_problems(event_spec(event="example_other.thing")))
    assert any("either a signal" in item for item in flows.spec_problems(event_spec(signal="ping")))
    assert any("payload field" in item for item in flows.spec_problems(event_spec(correlate="code")))


def test_existing_specifications_keep_their_digest():
    raw = approval_spec().to_json()
    for step in raw["steps"]:
        assert "event" not in step and "correlate" not in step  # New fields stay out while empty.
    assert event_spec().to_json()["steps"][1]["event"] == EVENT
    assert flows.FlowSpec.from_json(event_spec().to_json()) == event_spec()
