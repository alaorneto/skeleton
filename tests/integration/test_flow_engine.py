"""The flow engine without people (#203): gateways, joins, services, timers, signals and lifecycle.

PostgreSQL with the runtime role. Flows are registered for the test only, about notes of the
example app; jobs run through ``run_pending_jobs`` with the worker's real contexts.
"""

from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from skeleton.audit.models import AuditEvent
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.flows import registry as flows
from skeleton.flows import services
from skeleton.flows.errors import FlowConflict, FlowUnavailable
from skeleton.flows.models import FlowActivity, FlowDefinition, FlowInstance, FlowStepRun, FlowSubmission
from skeleton.jobs.models import Job
from skeleton.testing.events import capture_events
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import _set_enabled
from tools.security_fixtures import actor_tenant, build_security_fixture

OWNER = "example_notes"
START = flows.Form(
    flows.Field("amount", "decimal"),
    flows.Field("flag", "bool"),
    flows.Field("secret", "str", sensitive=True),
)
CONFIRM = flows.Form(flows.Field("reference", "str", required=True, max_length=20))
RESULT = flows.Form(flows.Field("ticket", "str"))


def auto_spec(version=1, **overrides):
    values = {
        "key": "example_notes.auto",
        "version": version,
        "label": "Automático",
        "owner_app": OWNER,
        "subject_table": "example_notes.note",
        "start_form": START,
        "steps": (
            flows.start("start"),
            flows.exclusive("route"),
            flows.service("heavy", handler="example_notes.heavy", outputs=RESULT, max_attempts=2, retry_seconds=1),
            flows.timer("pause", delay_seconds=0),
            flows.wait("confirm", signal="confirmed", timeout_seconds=3600, form=CONFIRM),
            flows.end("done", outcome="done"),
            flows.end("timed_out", outcome="timed_out"),
        ),
        "transitions": (
            flows.Transition("start", "route"),
            flows.Transition("route", "heavy", condition=flows.gt("amount", 100)),
            flows.Transition("route", "pause", default=True),
            flows.Transition("heavy", "confirm"),
            flows.Transition("pause", "confirm"),
            flows.Transition("confirm", "done", on="received"),
            flows.Transition("confirm", "timed_out", on="timeout"),
        ),
    }
    values.update(overrides)
    return flows.FlowSpec(**values)


def blocks_spec():
    return flows.FlowSpec(
        key="example_notes.blocks",
        version=1,
        label="Blocos",
        owner_app=OWNER,
        subject_table="example_notes.note",
        start_form=START,
        steps=(
            flows.start("start"),
            flows.parallel("fork", join="joined"),
            flows.service("left", handler="example_notes.left"),
            flows.service("right", handler="example_notes.right"),
            flows.join("joined"),
            flows.inclusive("extras", join="extras_done"),
            flows.timer("flagged", delay_seconds=0),
            flows.service("expensive", handler="example_notes.left"),
            flows.timer("plain", delay_seconds=0),
            flows.join("extras_done"),
            flows.end("done", outcome="done"),
        ),
        transitions=(
            flows.Transition("start", "fork"),
            flows.Transition("fork", "left"),
            flows.Transition("fork", "right"),
            flows.Transition("left", "joined"),
            flows.Transition("right", "joined"),
            flows.Transition("joined", "extras"),
            flows.Transition("extras", "flagged", condition=flows.truthy("flag")),
            flows.Transition("extras", "expensive", condition=flows.gt("amount", 100)),
            flows.Transition("extras", "plain", default=True),
            flows.Transition("flagged", "extras_done"),
            flows.Transition("expensive", "extras_done"),
            flows.Transition("plain", "extras_done"),
            flows.Transition("extras_done", "done"),
        ),
    )


class Handlers:
    def __init__(self):
        self.calls = []
        self.fail = set()

    def make(self, name, result=None):
        def handler(context):
            self.calls.append((name, context.step_key, dict(context.data)))
            if name in self.fail:
                raise RuntimeError("secret detail that must never be stored")
            return result

        return handler


@pytest.fixture
def world(skeleton_db, monkeypatch):
    from skeleton.flows import registry

    for name in ("_flows", "_handlers", "_conditions", "_assignments"):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(registry, "_duplicates", [])
    handlers = Handlers()
    flows.register_flow_handler(
        "example_notes.heavy",
        handlers.make("heavy", flows.StepResult(data={"ticket": "T-1"})),
        owner_app=OWNER,
    )
    flows.register_flow_handler("example_notes.left", handlers.make("left"), owner_app=OWNER)
    flows.register_flow_handler("example_notes.right", handlers.make("right"), owner_app=OWNER)
    flows.register_flow(auto_spec())
    flows.register_flow(blocks_spec())
    assert flows.flow_problems() == []
    fixture = build_security_fixture()
    fixture.handlers = handlers
    return fixture


def start(world, flow="example_notes.auto", note=None, user=None, **kwargs):
    with actor_tenant(user or world.alice, world.a):
        instance, created = services.start_instance(
            flow, subject_id=(note or world.private).pk, actor=user or world.alice, **kwargs
        )
        return instance.pk, created


def state(world, instance_id):
    with actor_tenant(world.admin, world.a):
        instance = FlowInstance.objects.get(pk=instance_id)
        runs = list(FlowStepRun.objects.filter(instance=instance).order_by("sequence"))
        return instance, runs


def history(world, instance_id):
    with actor_tenant(world.admin, world.a):
        return list(
            FlowActivity.objects.filter(instance_id=instance_id).order_by("sequence").values_list("kind", "step_key")
        )


def signal(world, instance_id, name="confirmed", user=None, **data):
    with actor_tenant(user or world.alice, world.a):
        return services.send_signal(instance_id, name, actor=user or world.alice, data=data)


def command(world, function, instance_id, user=None, **kwargs):
    instance, _ = state(world, instance_id)
    kwargs.setdefault("expected_revision", instance.revision)
    with actor_tenant(user or world.admin, world.a):
        return function(instance_id, actor=user or world.admin, **kwargs)


def test_default_route_timer_wait_and_end(world):
    with capture_events() as events:
        instance_id, created = start(world, data={"amount": "50", "secret": "s3cr3t"})
    assert created and [event.event_type for event in events] == ["flows.instance_started"]
    assert events[0].data["flow_key"] == "example_notes.auto" and "secret" not in str(events[0].data)
    instance, runs = state(world, instance_id)
    assert instance.status == "running" and instance.starter_id == world.alice.pk
    assert (instance.data, instance.sensitive_data) == ({"amount": "50"}, {"secret": "s3cr3t"})
    assert [(run.step_key, run.status) for run in runs] == [
        ("start", "completed"),
        ("route", "completed"),
        ("pause", "active"),
    ]
    assert [outcome.state for outcome in run_pending_jobs()] == ["succeeded"]
    instance, runs = state(world, instance_id)
    assert [(run.step_key, run.status) for run in runs][-1] == ("confirm", "active")
    with pytest.raises(ValidationError):
        signal(world, instance_id)  # The wait's form requires the reference.
    with pytest.raises(ValidationError):
        signal(world, instance_id, reference="R-1", unknown="x")
    assert signal(world, instance_id, "something_else", reference="R-1") is False
    with capture_events() as events:
        assert signal(world, instance_id, reference="R-1") is True
    assert [event.event_type for event in events] == ["flows.instance_completed"]
    assert signal(world, instance_id, reference="R-1") is False  # Nothing waits any more.
    instance, runs = state(world, instance_id)
    assert (instance.status, instance.outcome) == ("completed", "done") and instance.finished_at is not None
    assert instance.data == {"amount": "50", "reference": "R-1"}
    assert all(run.status == "completed" for run in runs)
    assert history(world, instance_id) == [
        ("instance_started", ""),
        ("gateway", "route"),
        ("step_entered", "pause"),
        ("step_completed", "pause"),
        ("step_entered", "confirm"),
        ("signal_received", "confirm"),
        ("step_completed", "confirm"),
        ("instance_completed", "done"),
    ]
    with actor_tenant(world.admin, world.a):
        origins = FlowSubmission.objects.filter(instance_id=instance_id).order_by("created_at")
        assert [item.origin for item in origins] == ["start", "signal"]
        activity = FlowActivity.objects.filter(instance_id=instance_id).order_by("sequence").first()
        assert activity.created_by == world.alice.pk  # Who did it comes from the context, not from input.


def test_condition_routes_to_the_service_and_outputs_join_the_data(world):
    instance_id, _ = start(world, data={"amount": "100.01"})
    _, runs = state(world, instance_id)
    assert runs[-1].step_key == "heavy" and runs[-1].status == "active"
    run_pending_jobs()
    instance, runs = state(world, instance_id)
    assert world.handlers.calls == [("heavy", "heavy", {"amount": "100.01"})]
    assert instance.data == {"amount": "100.01", "ticket": "T-1"}
    assert runs[-1].step_key == "confirm"
    with actor_tenant(world.admin, world.a):
        # The service ran as the system, in a job of this organization.
        event = AuditEvent.objects.filter(model_label="sk_flows.flowsubmission").order_by("-created_at").first()
        assert event.source == "system:job:flows.service" and event.actor_sysid is None


def test_wait_timeout_takes_the_timeout_transition(world):
    instance_id, _ = start(world)
    run_pending_jobs()
    assert run_pending_jobs() == []  # The timeout job is scheduled for later.
    Job.all_objects.filter(job_type="flows.wait_timeout").update(run_after=timezone.now())
    run_pending_jobs()
    instance, _ = state(world, instance_id)
    assert (instance.status, instance.outcome) == ("completed", "timed_out")
    assert signal(world, instance_id, reference="late") is False


def test_parallel_join_waits_for_every_branch_and_inclusive_join_only_for_the_activated(world):
    instance_id, _ = start(world, "example_notes.blocks", data={"flag": True, "amount": "500"})
    _, runs = state(world, instance_id)
    assert {run.step_key for run in runs if run.status == "active"} == {"left", "right"}
    fork = next(run for run in runs if run.step_key == "fork")
    assert fork.state == {"branches": ["fork>left", "fork>right"]}
    assert {run.scope_id for run in runs if run.step_key in ("left", "right")} == {fork.pk}

    from skeleton.jobs.worker import Worker

    Worker(worker_id="one", queues=("default",)).run_once()  # Only one branch finishes.
    instance, runs = state(world, instance_id)
    joined = [run for run in runs if run.step_key == "joined"]
    assert [run.status for run in joined] == ["waiting"] and len(joined[0].state["arrived"]) == 1
    assert not any(run.step_key == "extras" for run in runs)

    run_pending_jobs()
    instance, runs = state(world, instance_id)
    assert [run.status for run in runs if run.step_key == "joined"] == ["completed"]  # Fired exactly once.
    extras = next(run for run in runs if run.step_key == "extras")
    assert extras.state == {"branches": ["extras>flagged", "extras>expensive"]}  # Not the default one.
    assert not any(run.step_key == "plain" for run in runs)
    assert (instance.status, instance.outcome) == ("completed", "done")
    assert [run.status for run in runs if run.step_key == "extras_done"] == ["completed"]
    assert [run.scope_id for run in runs if run.step_key == "done"] == [None]  # The token left both blocks.
    assert sorted(name for name, _, _ in world.handlers.calls) == ["left", "left", "right"]


def test_inclusive_gateway_falls_back_to_the_default_branch(world):
    instance_id, _ = start(world, "example_notes.blocks", data={"amount": "1"})
    run_pending_jobs()
    instance, runs = state(world, instance_id)
    assert next(run for run in runs if run.step_key == "extras").state == {"branches": ["extras>plain"]}
    assert instance.status == "completed"


def test_running_instances_stay_on_the_version_they_were_born_in(world, monkeypatch):
    from skeleton.flows import registry

    first, _ = start(world)
    run_pending_jobs()
    changed = auto_spec(
        version=2,
        steps=(*auto_spec().steps[:-2], flows.end("done", outcome="done_v2"), flows.end("timed_out")),
    )
    monkeypatch.setitem(registry._flows, changed.key, changed)
    second, _ = start(world, note=world.shared)
    run_pending_jobs()
    with actor_tenant(world.admin, world.a):
        assert sorted(FlowDefinition.objects.values_list("version", flat=True)) == [1, 2]
    assert signal(world, first, reference="R") and signal(world, second, reference="R")
    assert state(world, first)[0].outcome == "done" and state(world, first)[0].flow_version == 1
    assert state(world, second)[0].outcome == "done_v2" and state(world, second)[0].flow_version == 2

    # Changing the flow without a new version is a publication error: nothing starts.
    tampered = auto_spec(version=2, label="Outro rótulo")
    monkeypatch.setitem(registry._flows, tampered.key, tampered)
    with pytest.raises(FlowConflict) as refused:
        start(world, note=world.grouped)
    assert refused.value.code == "definition_changed"
    with actor_tenant(world.admin, world.a):
        assert not FlowInstance.objects.filter(subject_sysid=world.grouped.pk).exists()


def test_idempotency_key_and_one_active_instance_per_subject(world):
    first, created = start(world, idempotency_key="order-1")
    again, created_again = start(world, idempotency_key="order-1")
    assert created and not created_again and again == first
    with pytest.raises(FlowConflict) as mismatch:
        start(world, note=world.shared, idempotency_key="order-1")
    assert mismatch.value.code == "idempotency_mismatch"
    with pytest.raises(FlowConflict) as running:
        start(world)
    assert running.value.code == "already_running"
    other, _ = start(world, "example_notes.blocks")  # Another flow about the same record is fine.
    assert other != first
    run_pending_jobs()
    signal(world, first, reference="R")
    assert start(world, idempotency_key="order-2")[1] is True  # The finished one frees the record.
    with pytest.raises(ValidationError):
        start(world, note=world.shared, idempotency_key="")


def test_caller_rollback_leaves_nothing(world):
    def counts():
        with actor_tenant(world.admin, world.a):
            return (
                FlowInstance.objects.count(),
                FlowDefinition.objects.count(),
                AuditEvent.objects.filter(model_label__startswith="sk_flows.").count(),
                Job.all_objects.filter(job_type__startswith="flows.").count(),
            )

    before = counts()
    with capture_events():
        with actor_tenant(world.alice, world.a):
            with pytest.raises(RuntimeError), transaction.atomic():
                services.start_instance("example_notes.auto", subject_id=world.private.pk, actor=world.alice)
                raise RuntimeError("caller failed")
    assert counts() == before
    assert run_pending_jobs() == []


def test_start_authorization_is_default_deny(world):
    from skeleton.flows.services import NotVisible

    def refused(error, user, subject_id, flow="example_notes.auto"):
        with actor_tenant(user, world.a):
            with pytest.raises(error) as raised:
                services.start_instance(flow, subject_id=subject_id, actor=user)
        return raised.value

    missing = refused(NotVisible, world.alice, uuid4())
    foreign = refused(NotVisible, world.alice, world.foreign.pk)  # A note of organization B.
    hidden = refused(NotVisible, world.bob, world.private.pk)  # Bob cannot read Alice's private note.
    assert str(missing) == str(foreign) == str(hidden)
    refused(PermissionDenied, world.bob, world.shared.pk)  # Readable, but starting needs update.
    refused(ValidationError, world.alice, world.private.pk, flow="example_notes.unknown")
    refused(ValidationError, world.alice, "not-a-uuid")
    with actor_tenant(world.alice, world.a):
        with pytest.raises(PermissionDenied):  # No actor outside a system context.
            services.start_instance("example_notes.auto", subject_id=world.private.pk, actor=None)
        assert not FlowInstance.objects.exists()
    # In B, Alice is a member without table permission: the same answer as a missing note.
    with actor_tenant(world.alice, world.b):
        with pytest.raises(NotVisible):
            services.start_instance("example_notes.auto", subject_id=world.foreign.pk, actor=world.alice)


def test_instances_of_one_organization_are_invisible_in_the_other(world):
    from skeleton.flows import policies
    from skeleton.flows.services import NotVisible

    instance_id, _ = start(world)
    with actor_tenant(world.bob, world.b):  # Bob administers B.
        assert not policies.visible_instances(world.bob).exists()
        with pytest.raises(NotVisible):
            services.send_signal(instance_id, "confirmed", actor=world.bob, data={"reference": "R"})
        with pytest.raises(NotVisible):
            services.cancel_instance(instance_id, actor=world.bob, expected_revision=1)
    with actor_tenant(world.bob, world.a):  # In A he is a plain member who cannot read the private note.
        assert not policies.visible_instances(world.bob).exists()
        with pytest.raises(NotVisible):
            services.cancel_instance(instance_id, actor=world.bob, expected_revision=1)
    with actor_tenant(world.alice, world.a):
        assert list(policies.visible_instances(world.alice).values_list("pk", flat=True)) == [instance_id]
    with actor_tenant(world.admin, world.a):
        assert policies.visible_instances(world.admin).count() == 1


def test_disabled_app_blocks_and_enabling_it_resumes(world):
    from skeleton.flows import policies

    instance_id, _ = start(world)  # Waiting on the zero-second timer.
    with actor_tenant(world.admin, world.a):
        _set_enabled(OWNER, world.admin, enabled=False)
    with actor_tenant(world.alice, world.a):
        with pytest.raises(FlowUnavailable):
            services.start_instance("example_notes.auto", subject_id=world.shared.pk, actor=world.alice)
        with pytest.raises(FlowUnavailable):
            services.send_signal(instance_id, "confirmed", actor=world.alice, data={"reference": "R"})
        assert not policies.visible_instances(world.alice).exists()
    with actor_tenant(world.admin, world.a):
        with pytest.raises(FlowUnavailable):
            services.suspend_instance(instance_id, actor=world.admin, expected_revision=1)
    run_pending_jobs()  # The timer fires while the app is off: nothing moves.
    _, runs = state(world, instance_id)
    assert runs[-1].step_key == "pause" and runs[-1].status == "active"
    with actor_tenant(world.admin, world.a):
        _set_enabled(OWNER, world.admin, enabled=True)
    run_pending_jobs()  # app.enabled reschedules the timer; the new job moves the flow on.
    _, runs = state(world, instance_id)
    assert runs[-1].step_key == "confirm"


def test_failing_handler_is_retried_then_fails_and_retry_resumes(world):
    world.handlers.fail.add("heavy")
    instance_id, _ = start(world, data={"amount": "999"})
    run_pending_jobs()
    instance, runs = state(world, instance_id)
    assert instance.status == "running" and runs[-1].state["attempts"] == 1
    Job.all_objects.filter(job_type="flows.service", state="queued").update(run_after=timezone.now())
    with capture_events() as events:
        run_pending_jobs()
    assert [event.event_type for event in events] == ["flows.instance_failed"]
    instance, runs = state(world, instance_id)
    assert instance.status == "failed" and runs[-1].status == "failed"
    with actor_tenant(world.admin, world.a):
        failure = FlowActivity.objects.get(instance_id=instance_id, kind="instance_failed")
        assert failure.detail == {"code": "handler_error", "error_class": "RuntimeError"}
        assert "secret detail" not in str(list(FlowActivity.objects.values_list("detail", "comment")))
    assert signal(world, instance_id, reference="R") is False
    with pytest.raises(FlowConflict):
        command(world, services.suspend_instance, instance_id)

    world.handlers.fail.clear()
    command(world, services.retry_instance, instance_id)
    run_pending_jobs()
    instance, runs = state(world, instance_id)
    assert instance.status == "running" and runs[-1].step_key == "confirm"
    assert instance.data["ticket"] == "T-1"
    with pytest.raises(FlowConflict):
        command(world, services.retry_instance, instance_id)


def test_missing_handler_fails_the_instance_instead_of_running_something_else(world, monkeypatch):
    from skeleton.flows import registry

    instance_id, _ = start(world, data={"amount": "999"})
    monkeypatch.delitem(registry._handlers, "example_notes.heavy")
    run_pending_jobs()
    instance, _ = state(world, instance_id)
    assert instance.status == "failed"
    with actor_tenant(world.admin, world.a):
        assert FlowActivity.objects.get(instance_id=instance_id, kind="instance_failed").detail == {
            "code": "handler_missing"
        }
    assert world.handlers.calls == []


def test_suspend_freezes_the_instance_and_resume_reschedules(world):
    instance_id, _ = start(world)
    command(world, services.suspend_instance, instance_id, reason="aguardando orçamento")
    assert run_pending_jobs() != []  # The timer job runs and does nothing.
    instance, runs = state(world, instance_id)
    assert instance.status == "suspended" and runs[-1].step_key == "pause" and runs[-1].status == "active"
    with pytest.raises(FlowConflict) as refused:
        signal(world, instance_id, reference="R")
    assert refused.value.code == "instance_suspended"
    with pytest.raises(FlowConflict):
        command(world, services.suspend_instance, instance_id)
    command(world, services.resume_instance, instance_id)
    run_pending_jobs()
    instance, runs = state(world, instance_id)
    assert instance.status == "running" and runs[-1].step_key == "confirm"
    with pytest.raises(FlowConflict):
        command(world, services.resume_instance, instance_id)
    with actor_tenant(world.admin, world.a):
        suspended = FlowActivity.objects.get(instance_id=instance_id, kind="instance_suspended")
        assert suspended.comment == "aguardando orçamento" and suspended.created_by == world.admin.pk
        audit = AuditEvent.objects.get(record_sysid=suspended.pk)
        assert audit.changes["comment"]["new"] == "[REDACTED]"


def test_cancel_closes_everything_and_late_jobs_do_nothing(world):
    instance_id, _ = start(world, "example_notes.blocks")
    with capture_events() as events:
        command(world, services.cancel_instance, instance_id, user=world.alice, reason="desisti")  # The starter.
    assert [event.event_type for event in events] == ["flows.instance_cancelled"]
    instance, runs = state(world, instance_id)
    assert instance.status == "cancelled" and instance.finished_at is not None
    assert {run.status for run in runs if run.step_key in ("left", "right")} == {"cancelled"}
    run_pending_jobs()
    assert world.handlers.calls == []
    assert state(world, instance_id)[0].status == "cancelled"
    with pytest.raises(FlowConflict) as closed:
        command(world, services.cancel_instance, instance_id)
    assert closed.value.code == "instance_closed"
    assert start(world, "example_notes.blocks")[1] is True  # The record is free again.


def test_lifecycle_commands_require_the_administrator_and_the_current_revision(world, monkeypatch):
    from skeleton.flows import registry

    instance_id, _ = start(world)
    for function in (services.suspend_instance, services.resume_instance, services.retry_instance):
        with pytest.raises(PermissionDenied):
            command(world, function, instance_id, user=world.alice)  # She reads it, but does not administer.
    stale = state(world, instance_id)[0].revision - 1
    for function in (services.suspend_instance, services.cancel_instance):
        with pytest.raises(ConcurrentUpdate):
            command(world, function, instance_id, expected_revision=stale)
        with pytest.raises(ValidationError):
            command(world, function, instance_id, expected_revision="1")
    assert state(world, instance_id)[0].status == "running"
    # Two administrators' commands at the same revision: the first wins, the second is stale.
    revision = state(world, instance_id)[0].revision
    command(world, services.suspend_instance, instance_id, expected_revision=revision)
    with pytest.raises(ConcurrentUpdate):
        command(world, services.resume_instance, instance_id, expected_revision=revision)

    locked = auto_spec(version=3, starter_can_cancel=False)
    monkeypatch.setitem(registry._flows, locked.key, locked)
    other, _ = start(world, note=world.shared)
    with pytest.raises(PermissionDenied):
        command(world, services.cancel_instance, other, user=world.alice)
    command(world, services.cancel_instance, other)


def test_system_context_may_start_and_signal_without_an_actor(world):
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:flow-system", organization_id=world.a.pk):
        instance, created = services.start_instance("example_notes.auto", subject_id=world.private.pk, actor=None)
        assert created and instance.starter_id is None
    run_pending_jobs()
    with system_context(reason="test:flow-system", organization_id=world.a.pk):
        assert services.send_signal(instance.pk, "confirmed", actor=None, data={"reference": "R"}) is True
    assert state(world, instance.pk)[0].status == "completed"
