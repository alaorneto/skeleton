"""Steps nobody has to act on: services, timers and waits, driven by jobs (docs/flows.md, "Execução").

Every job carries only the run identifier and a *ticket*. A step that is (re)scheduled takes a
new ticket, so a job that fires late (after a suspension, a retry or a duplicate delivery)
sees a different ticket, or a run that is no longer active, and does nothing. The handler
of a service, its declared outputs and the advance of the flow commit together.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .engine import Behavior, Execution, lock_instance, register_behavior
from .errors import FlowFailure
from .models import FlowStepRun
from .registry import StepResult, get_handler
from .spec import Step

SERVICE_JOB = "flows.service"
TIMER_JOB = "flows.timer"
WAIT_TIMEOUT_JOB = "flows.wait_timeout"
MAX_RETRY_DELAY = 86400


def schedule(execution: Execution, run: FlowStepRun, job: str, *, run_after: datetime | None = None) -> None:
    """Enqueue ``job`` for ``run`` with a fresh ticket, in the transaction that changes the run."""
    from skeleton.jobs.services import enqueue

    ticket = int(run.state.get("ticket", 0)) + 1
    run.state = {**run.state, "ticket": ticket}
    run.save(update_fields={"state", "due_at"})
    enqueue(
        job,
        organization_id=run.organization_id,
        payload={"run_id": str(run.pk), "ticket": ticket},
        idempotency_key=f"flow:{job}:{run.pk}:{ticket}",
        run_after=run_after,
    )


def app_enabled(app_label: str) -> bool:
    from skeleton.system.applications import require_application_enabled

    try:
        require_application_enabled(app_label)
    except (PermissionDenied, KeyError):
        return False
    return True


def claim_run(run_id: Any, ticket: int, kind: str) -> tuple[Execution, FlowStepRun, Step] | None:
    """Lock the instance of a scheduled run; ``None`` when the job is stale or the flow is paused.

    A paused flow (suspended instance, disabled app) leaves the run as it is: resuming, or
    enabling the app again, schedules it with a new ticket.
    """
    seen = FlowStepRun.objects.filter(pk=run_id).values_list("instance_id", flat=True).first()
    if seen is None:
        return None
    instance = lock_instance(seen)
    if instance is None or instance.status != "running":
        return None
    run = FlowStepRun.objects.filter(pk=run_id, status="active", kind=kind).first()
    if run is None or int(run.state.get("ticket", 0)) != ticket:
        return None
    if not app_enabled(instance.definition.app_label):
        return None
    execution = Execution(instance)
    return execution, run, execution.spec.step(run.step_key)


def advance(execution: Execution, run: FlowStepRun, action: Callable[[], None]) -> None:
    """Run ``action`` atomically; a definition failure marks the instance failed instead of looping in the queue."""
    try:
        with transaction.atomic():
            action()
            execution.save()
    except FlowFailure as failure:
        execution.instance.refresh_from_db()
        failed = Execution(execution.instance)
        current = FlowStepRun.objects.filter(pk=run.pk).first()
        failed.fail(failure.code, step_key=failure.step_key, run=current)
        failed.finish()
        return
    execution.finish()


# Service steps.
def _enter_service(execution: Execution, run: FlowStepRun, step: Step) -> None:
    run.state = {**run.state, "attempts": 0}
    schedule(execution, run, SERVICE_JOB)


def _result(step: Step, returned: Any) -> tuple[str, dict[str, Any], dict[str, Any]]:
    from .forms import clean

    if returned is None:
        return "", {}, {}
    if isinstance(returned, str):
        returned = StepResult(outcome=returned)
    if not isinstance(returned, StepResult) or not isinstance(returned.outcome, str) or len(returned.outcome) > 64:
        raise FlowFailure("handler_result", step.key)
    if not returned.data:
        return returned.outcome, {}, {}
    if step.outputs is None:
        raise FlowFailure("handler_result", step.key)
    try:
        values, sensitive = clean((step.outputs,), dict(returned.data))
    except ValidationError:
        raise FlowFailure("handler_result", step.key) from None
    return returned.outcome, values, sensitive


def run_service(*, run_id: str, ticket: int) -> None:
    """Job handler (system context of the organization): call the app's handler and move on."""
    claimed = claim_run(run_id, ticket, "service")
    if claimed is None:
        return
    execution, run, step = claimed
    try:
        entry = get_handler(step.handler)
        if entry.owner_app != execution.spec.owner_app:
            raise KeyError(step.handler)
    except KeyError:
        execution.fail("handler_missing", run=run)
        execution.finish()
        return
    attempt = int(run.state.get("attempts", 0)) + 1
    error: Exception | None = None
    try:
        with transaction.atomic():
            outcome, values, sensitive = _result(step, entry.function(execution.context(step.key)))
            execution.submit("service", values, sensitive, run=run)
            execution.complete(run, outcome)
            execution.save()
    except FlowFailure as failure:
        error = failure
    except Exception as crash:  # noqa: BLE001 - a handler may raise anything; its writes are rolled back
        error = crash
    if error is None:
        execution.finish()
        return
    # The savepoint undid whatever the handler and the advance wrote: start again from the stored state.
    execution.instance.refresh_from_db()
    execution = Execution(execution.instance)
    run = FlowStepRun.objects.get(pk=run.pk)
    run.state = {**run.state, "attempts": attempt}
    if isinstance(error, FlowFailure):
        run.save(update_fields={"state"})
        execution.fail(error.code, step_key=error.step_key, run=run)
    elif attempt >= step.max_attempts:
        run.save(update_fields={"state"})
        execution.fail("handler_error", run=run, error_class=type(error).__name__)
    else:
        delay = min(step.retry_seconds * 2 ** (attempt - 1), MAX_RETRY_DELAY)
        execution.log("service_retry", run=run, detail={"attempt": attempt, "error_class": type(error).__name__[:200]})
        schedule(execution, run, SERVICE_JOB, run_after=execution.now + timedelta(seconds=delay))
    execution.finish()


def _resume_service(execution: Execution, run: FlowStepRun, step: Step) -> None:
    run.state = {**run.state, "attempts": 0}
    schedule(execution, run, SERVICE_JOB)


# Timers.
def _enter_timer(execution: Execution, run: FlowStepRun, step: Step) -> None:
    run.due_at = execution.now + timedelta(seconds=step.delay_seconds)
    schedule(execution, run, TIMER_JOB, run_after=run.due_at)


def _resume_timer(execution: Execution, run: FlowStepRun, step: Step) -> None:
    schedule(execution, run, TIMER_JOB, run_after=max(run.due_at or execution.now, execution.now))


def fire_timer(*, run_id: str, ticket: int) -> None:
    claimed = claim_run(run_id, ticket, "timer")
    if claimed is None:
        return
    execution, run, _ = claimed
    advance(execution, run, lambda: execution.complete(run))


# Waits.
def _enter_wait(execution: Execution, run: FlowStepRun, step: Step) -> None:
    if step.timeout_seconds:
        run.due_at = execution.now + timedelta(seconds=step.timeout_seconds)
        schedule(execution, run, WAIT_TIMEOUT_JOB, run_after=run.due_at)


def _resume_wait(execution: Execution, run: FlowStepRun, step: Step) -> None:
    if run.due_at is not None:
        schedule(execution, run, WAIT_TIMEOUT_JOB, run_after=max(run.due_at, execution.now))


def expire_wait(*, run_id: str, ticket: int) -> None:
    claimed = claim_run(run_id, ticket, "wait")
    if claimed is None:
        return
    execution, run, _ = claimed
    advance(execution, run, lambda: execution.complete(run, "timeout"))


def resume_application(envelope: Any) -> None:
    """Consumer of ``app.enabled``: reschedule what the flows of that app were waiting for.

    Jobs that fired while the app was disabled did nothing; without this the instances would
    stay where they were.
    """
    from .models import FlowInstance

    label = envelope.data.get("app_label")
    ids = list(
        FlowInstance.objects.filter(definition__app_label=label, status="running")
        .order_by("created_at")
        .values_list("pk", flat=True)[:1000]
    )
    for instance_id in ids:
        instance = lock_instance(instance_id)
        if instance is None or instance.status != "running":
            continue
        execution = Execution(instance)
        execution.resume_runs()
        execution.finish()


def register_automatic_steps() -> None:
    from skeleton.events.core_events import APP_ENABLED
    from skeleton.events.registry import register_consumer
    from skeleton.jobs.registry import JobType, register_job_type

    register_behavior("service", Behavior(enter=_enter_service, resume=_resume_service))
    register_behavior("timer", Behavior(enter=_enter_timer, resume=_resume_timer))
    register_behavior("wait", Behavior(enter=_enter_wait, resume=_resume_wait))
    schema = {"run_id": "uuid", "ticket": "int"}
    for code, handler in ((SERVICE_JOB, run_service), (TIMER_JOB, fire_timer), (WAIT_TIMEOUT_JOB, expire_wait)):
        register_job_type(JobType(code, handler, schema, run_as="system", owner_app="sk_flows"))
    register_consumer(APP_ENABLED, "flows.resume_application", resume_application)
