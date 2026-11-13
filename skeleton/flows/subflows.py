"""Subflows (#412): a step that starts another flow on the same record and waits for its end.

The child runs at the version the parent pinned (``subflow(..., version=N)``): publishing a
new version of the child never changes a parent in progress. The child starts inside the
parent's command (lock order parent, then child); its end reaches the parent through a job,
so the parent is never locked from within the child's command. A completed child advances the
parent step with the child's outcome; a failed or cancelled child fails the parent step.
Cancelling the parent cancels the child, suspending suspends it and resuming resumes it.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

from .engine import Behavior, Execution, lock_instance, register_behavior
from .errors import FlowFailure
from .models import ACTIVE_STATES, FlowDefinition, FlowInstance, FlowStepRun
from .spec import Step

SUBFLOW_JOB = "flows.subflow_finished"
TERMINAL_MOMENTS = ("completed", "failed", "cancelled")


def _definition(step: Step) -> Any:
    from .definitions import published_data_spec
    from .registry import get_flow
    from .services import ensure_definition

    definition = FlowDefinition.objects.filter(key=step.flow, version=step.flow_version).first()
    if definition is not None:
        return definition
    try:
        spec = get_flow(step.flow)
    except KeyError:
        spec = published_data_spec(step.flow)
    if spec is None or spec.version != step.flow_version:
        raise FlowFailure("subflow_version_unavailable", step.key)
    return ensure_definition(spec)


def _enter(execution: Execution, run: FlowStepRun, step: Step) -> None:
    from . import forms

    parent = execution.instance
    definition = _definition(step)
    busy = FlowInstance.objects.filter(
        flow_key=step.flow, subject_sysid=parent.subject_sysid, status__in=ACTIVE_STATES
    ).exists()
    if busy:
        raise FlowFailure("subflow_busy", step.key)
    child = FlowInstance(
        organization_id=parent.organization_id,
        definition=definition,
        flow_key=step.flow,
        flow_version=step.flow_version,
        subject_table=parent.subject_table,
        subject_sysid=parent.subject_sysid,
        starter=None,
        parent=parent,
        parent_run=run,
    )
    child.save()
    started = Execution(child)
    try:
        values, sensitive = forms.clean((started.spec.start_form,), None)
    except ValidationError:
        raise FlowFailure("subflow_start_form", step.key) from None
    started.log(
        "instance_started", detail={"flow": started.spec.label, "version": step.flow_version, "parent": str(parent.pk)}
    )
    started.submit("start", values, sensitive)
    started.moments.append("started")
    started.activate(started.spec.start_step().key)
    started.finish()
    run.state = {**run.state, "child": str(child.pk)}
    run.save(update_fields={"state"})
    execution.log(
        "subflow_started", run=run, detail={"child": str(child.pk), "flow": step.flow, "version": step.flow_version}
    )


def _child(run: FlowStepRun) -> FlowInstance | None:
    child_id = run.state.get("child")
    return lock_instance(child_id) if child_id else None


def _cancel(execution: Execution, run: FlowStepRun, step: Step) -> None:
    child = _child(run)
    if child is None or child.status not in (*ACTIVE_STATES, "failed"):
        return
    closing = Execution(child)
    FlowStepRun.objects.filter(instance=child, status="failed").update(status="active")
    closing.cancel_runs()
    closing.touch(status="cancelled", finished_at=closing.now)
    closing.log("instance_cancelled", detail={"by": "parent"})
    closing.moments.append("cancelled")
    closing.finish()


def suspend_children(instance: FlowInstance) -> None:
    """The parent was suspended: the running children are suspended with it."""
    for child_id in (
        FlowInstance.objects.filter(parent=instance, status="running").order_by("sysid").values_list("pk", flat=True)
    ):
        child = lock_instance(child_id)
        if child is None or child.status != "running":
            continue
        pausing = Execution(child)
        pausing.touch(status="suspended")
        pausing.log("instance_suspended", detail={"by": "parent"})
        pausing.finish()


def _resume(execution: Execution, run: FlowStepRun, step: Step) -> None:
    child = _child(run)
    if child is None:
        return
    if child.status == "suspended":
        resuming = Execution(child)
        resuming.touch(status="running")
        resuming.log("instance_resumed", detail={"by": "parent"})
        resuming.resume_runs()
        resuming.finish()
    elif child.status not in ACTIVE_STATES:
        notify_parent(child)  # It ended while the parent was paused: deliver the end now.


def notify_parent(child: FlowInstance) -> None:
    """Enqueue the end of a child for its parent, in the child's transaction."""
    from skeleton.jobs.services import enqueue

    revision = FlowStepRun.objects.filter(pk=child.parent_run_id).values_list("revision", flat=True).first()
    enqueue(
        SUBFLOW_JOB,
        organization_id=child.organization_id,
        payload={"run_id": str(child.parent_run_id), "child_id": str(child.pk)},
        idempotency_key=f"flow:subflow:{child.parent_run_id}:{child.pk}:{child.status}:{revision}",
    )


def subflow_finished(*, run_id: str, child_id: str) -> None:
    """Job handler (system context): move the parent step on with the child's result."""
    from .automatic import advance

    seen = FlowStepRun.objects.filter(pk=run_id).values_list("instance_id", flat=True).first()
    if seen is None:
        return
    instance = lock_instance(seen)
    if instance is None or instance.status != "running":
        return  # Paused or closed: resuming delivers the end again.
    run = FlowStepRun.objects.filter(pk=run_id, status="active", kind="subflow").first()
    child = FlowInstance.objects.filter(pk=child_id, parent_run_id=run_id).first()
    if run is None or child is None or child.status in ACTIVE_STATES:
        return
    execution = Execution(instance)
    if child.status == "completed":
        advance(execution, run, lambda: execution.complete(run, child.outcome))
        return
    execution.fail(f"subflow_{child.status}", run=run)
    execution.finish()


def register_subflows() -> None:
    from skeleton.jobs.registry import JobType, register_job_type

    register_behavior("subflow", Behavior(enter=_enter, cancel=_cancel, resume=_resume))
    register_job_type(
        JobType(
            SUBFLOW_JOB, subflow_finished, {"run_id": "uuid", "child_id": "uuid"}, run_as="system", owner_app="sk_flows"
        )
    )
