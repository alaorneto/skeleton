"""Subflows (#412): the parent waits for the child at the pinned version, and its lifecycle propagates."""

import pytest

from examples.notes.models import Note
from skeleton.flows import registry as flows
from skeleton.service_api.flows import FlowService
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import build_service_scenario
from tests.integration.test_flow_service_api import as_app

pytestmark = pytest.mark.needs_example_app
service = FlowService()
PARENT, CHILD = "example_notes.parent", "example_notes.legal"


def child_spec(version=1, outcome="approved"):
    return flows.FlowSpec(
        key=CHILD,
        version=version,
        label=f"Jurídico v{version}",
        owner_app="example_notes",
        subject_table="example_notes.note",
        steps=(flows.start("start"), flows.wait("ok", signal="ok"), flows.end("done", outcome=outcome)),
        transitions=(flows.Transition("start", "ok"), flows.Transition("ok", "done", on="received")),
    )


def parent_spec(**changes):
    return flows.FlowSpec(
        key=PARENT,
        version=1,
        label="Pai",
        owner_app="example_notes",
        subject_table="example_notes.note",
        steps=(
            flows.start("start"),
            flows.subflow("legal", flow=changes.get("flow", CHILD), version=changes.get("version", 1)),
            flows.end("done", outcome="approved"),
            flows.end("refused", outcome="refused"),
        ),
        transitions=(
            flows.Transition("start", "legal"),
            flows.Transition("legal", "done", on="approved"),
            flows.Transition("legal", "refused", on="rejected"),
        ),
    )


@pytest.fixture
def world(skeleton_db, monkeypatch):
    from skeleton.flows import registry

    for name in ("_flows", "_handlers", "_conditions", "_assignments"):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(registry, "_duplicates", [])
    flows.register_flow(child_spec())
    flows.register_flow(parent_spec())
    assert flows.flow_problems() == []
    built = build_service_scenario(Note, slug="subflows")
    built.note = built.create(built.a, code="s1", title="Com jurídico")
    return built


def begin(world):
    return world.caller(lambda actor: as_app(service.start)(actor=actor, flow_key=PARENT, subject_id=world.note.pk))()


def rows(world):
    from skeleton.flows.models import FlowInstance

    with system_context(reason="test:subflows", organization_id=world.a.pk):
        return {row.flow_key: row for row in FlowInstance.objects.filter(subject_sysid=world.note.pk)}


def admin(world, function):
    return world.caller(function, user=world.admin_a)()


def test_the_parent_waits_for_the_child_and_follows_its_outcome(world):
    begin(world)
    found = rows(world)
    child, parent = found[CHILD], found[PARENT]
    assert (parent.status, child.status, child.parent_id, child.flow_version) == ("running", "running", parent.pk, 1)
    world.caller(lambda actor: as_app(service.signal)(actor=actor, instance_id=child.pk, name="ok"))()
    run_pending_jobs()
    found = rows(world)
    assert (found[CHILD].status, found[PARENT].status, found[PARENT].outcome) == ("completed", "completed", "approved")


def test_cancel_suspend_and_resume_reach_the_child(world):
    begin(world)
    parent = rows(world)[PARENT]
    suspended = admin(
        world, lambda actor: service.suspend(actor=actor, instance_id=parent.pk, expected_revision=parent.revision)
    )
    assert rows(world)[CHILD].status == "suspended"
    resumed = admin(
        world, lambda actor: service.resume(actor=actor, instance_id=parent.pk, expected_revision=suspended.revision)
    )
    assert rows(world)[CHILD].status == "running"
    admin(world, lambda actor: service.cancel(actor=actor, instance_id=parent.pk, expected_revision=resumed.revision))
    run_pending_jobs()
    found = rows(world)
    assert (found[PARENT].status, found[CHILD].status) == ("cancelled", "cancelled")


def test_a_child_cancelled_on_its_own_fails_the_parent_step(world):
    begin(world)
    child = rows(world)[CHILD]
    admin(world, lambda actor: service.cancel(actor=actor, instance_id=child.pk, expected_revision=child.revision))
    run_pending_jobs()
    assert rows(world)[PARENT].status == "failed"


def test_the_pinned_version_holds_after_a_new_child_version(world, monkeypatch):
    from skeleton.flows import registry

    begin(world)
    first = rows(world)[CHILD]
    monkeypatch.setitem(registry._flows, CHILD, child_spec(version=2))  # The app ships v2 of the child.
    world.caller(lambda actor: as_app(service.signal)(actor=actor, instance_id=first.pk, name="ok"))()
    run_pending_jobs()
    assert rows(world)[PARENT].status == "completed"
    other = world.create(world.a, code="s2", title="Outra")
    world.caller(lambda actor: as_app(service.start)(actor=actor, flow_key=PARENT, subject_id=other.pk))()
    from skeleton.flows.models import FlowInstance

    with system_context(reason="test:subflows", organization_id=world.a.pk):
        assert FlowInstance.objects.get(flow_key=CHILD, subject_sysid=other.pk).flow_version == 1


def test_the_check_refuses_cycles_foreign_children_and_other_tables(world, monkeypatch):
    from skeleton.flows import registry

    looping = flows.FlowSpec(
        **{**child_spec().__dict__, "_index": {}}
        | {
            "steps": (
                flows.start("start"),
                flows.subflow("back", flow=PARENT, version=1),
                flows.end("done", outcome="approved"),
            ),
            "transitions": (flows.Transition("start", "back"), flows.Transition("back", "done", on="approved")),
        }
    )
    monkeypatch.setitem(registry._flows, CHILD, looping)
    assert any("subflow cycle" in item for item in flows.flow_problems())
    assert any("not declared in code" in item for item in flows.spec_problems(parent_spec(flow="example_notes.nope")))
    assert any("version from 1" in item for item in flows.spec_problems(parent_spec(version=0)))
