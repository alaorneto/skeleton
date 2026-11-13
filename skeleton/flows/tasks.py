"""Human steps: tasks, approvals, their queue, forms, decisions and deadlines (docs/flows.md).

One ``FlowTask`` exists per approver slot and the approval policy decides over the set. Every
command locks the instance, answers "not found" to whoever may not see the task, refuses
whoever may not act on it, compares the task revision and only then changes anything: two
people acting on the same task have one winner.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

from . import forms, policies
from .assignment import Slot, active_people, resolve
from .automatic import app_enabled
from .engine import Behavior, Execution, lock_instance, register_behavior
from .errors import FlowConflict, FlowFailure
from .models import FlowStepRun, FlowTask
from .services import NotVisible
from .spec import Step, Transition

REMINDER_JOB = "flows.task_reminder"
DUE_JOB = "flows.task_due"
DECISIONS = ("approved", "rejected")


# Opening and closing tasks.
def _open_task(execution: Execution, run: FlowStepRun, step: Step, slot: Slot | None, index: int) -> FlowTask:
    from .notifications import notify_task

    task = FlowTask(
        organization_id=run.organization_id,
        instance=execution.instance,
        step_run=run,
        step_key=step.key,
        kind=step.kind,
        slot=index,
        assignee_id=slot.user if slot else None,
        candidate_group_id=slot.group if slot else None,
        candidate_role_id=slot.role if slot else None,
        due_at=execution.now + timedelta(seconds=step.sla.due_seconds) if step.sla else None,
    )
    task.save()
    detail = slot.as_json() if slot else {}
    execution.log("task_created" if slot else "assignment_unresolved", run=run, task=task, detail=detail)
    if step.sla:
        _schedule_sla(execution, run, step, task)
    notify_task(execution, task, "flows.task_assigned")
    return task


def _schedule_sla(
    execution: Execution, run: FlowStepRun, step: Step, task: FlowTask, *, reminders: bool = True
) -> None:
    from skeleton.jobs.services import enqueue

    assert step.sla is not None and task.due_at is not None  # noqa: S101 - type narrowing; the caller only schedules steps with an SLA
    ticket = int(run.state.get("sla_ticket", 0))
    if reminders:
        for index, before in enumerate(sorted(step.sla.remind_before_seconds, reverse=True)):
            enqueue(
                REMINDER_JOB,
                organization_id=task.organization_id,
                payload={"task_id": str(task.pk), "index": index},
                idempotency_key=f"flow:reminder:{task.pk}:{index}",
                run_after=task.due_at - timedelta(seconds=before),
            )
    enqueue(
        DUE_JOB,
        organization_id=task.organization_id,
        payload={"task_id": str(task.pk), "ticket": ticket},
        idempotency_key=f"flow:due:{task.pk}:{ticket}",
        run_after=max(task.due_at, execution.now),
    )


def _close_task(task: FlowTask, status: str, **changes: Any) -> None:
    """Finish a task and drop its queue, so a closed task never keeps a group or a role from removal."""
    task.status, task.candidate_group_id, task.candidate_role_id = status, None, None
    for name, value in changes.items():
        setattr(task, name, value)
    task.save(update_fields={"status", "candidate_group", "candidate_role", *changes})


def _cancel_open(execution: Execution, run: FlowStepRun, *, keep: Any = None) -> None:
    for task in FlowTask.objects.select_for_update().filter(step_run=run, status="open").exclude(pk=keep):
        _close_task(task, "cancelled")
        execution.log("task_cancelled", run=run, task=task)


def _enter(execution: Execution, run: FlowStepRun, step: Step) -> None:
    slots = resolve(execution, step.key, step.assignees)
    if step.kind == "approval" and step.policy == "sequential" and slots:
        run.state = {**run.state, "slots": [slot.as_json() for slot in slots], "index": 0}
        run.save(update_fields={"state"})
        _open_task(execution, run, step, slots[0], 0)
        return
    for index, slot in enumerate(slots or [None]):
        _open_task(execution, run, step, slot, index)


def _cancel(execution: Execution, run: FlowStepRun, step: Step) -> None:
    _cancel_open(execution, run)


def _resume(execution: Execution, run: FlowStepRun, step: Step) -> None:
    """After a pause the due job gets a new ticket; reminders missed meanwhile are not replayed."""
    if not step.sla:
        return
    run.state = {**run.state, "sla_ticket": int(run.state.get("sla_ticket", 0)) + 1}
    run.save(update_fields={"state"})
    for task in FlowTask.objects.filter(step_run=run, status="open", escalated_at__isnull=True):
        _schedule_sla(execution, run, step, task, reminders=False)


# Commands of a person.
def _locked(task_id: Any, actor: Any) -> tuple[Execution, FlowTask]:
    """The task under the instance lock, if the actor may read it; anything else is ``NotVisible``."""
    try:
        identifier = task_id if isinstance(task_id, UUID) else UUID(str(task_id))
    except (ValueError, TypeError):
        raise ValidationError({"task_id": gettext("Identificador inválido.")}) from None
    instance_id = FlowTask.objects.filter(pk=identifier).values_list("instance_id", flat=True).first()
    instance = lock_instance(instance_id) if instance_id else None
    if instance is None:
        raise NotVisible()
    policies.require_flow_app(instance.definition.app_label)
    if not policies.visible_tasks(actor).filter(pk=identifier).exists():
        raise NotVisible()
    return Execution(instance), FlowTask.objects.select_for_update().get(pk=identifier)


def _ready(execution: Execution, task: FlowTask, actor: Any, expected_revision: Any, *, acting: bool = True) -> None:
    if acting and task.status == "open" and not policies.actionable_tasks(actor).filter(pk=task.pk).exists():
        raise PermissionDenied("The task belongs to someone else.")
    if type(expected_revision) is not int:
        raise ValidationError({"expected_revision": gettext("Use a revisão inteira atual.")})
    if task.revision != expected_revision:
        raise ConcurrentUpdate("The task changed.")
    if task.status != "open":
        # First: it is the precise reason for whoever lost the race on a queue task.
        raise FlowConflict("task_closed", gettext("Esta tarefa já foi encerrada, possivelmente por outra pessoa."))
    status = execution.instance.status
    if status == "suspended":
        raise FlowConflict("instance_suspended", gettext("A instância está suspensa."))
    if status != "running":
        raise FlowConflict("instance_closed", gettext("A instância não está em andamento."))


def claim_task(task_id: Any, *, actor: Any, expected_revision: int) -> FlowTask:
    execution, task = _locked(task_id, actor)
    _ready(execution, task, actor, expected_revision)
    if task.assignee_id is not None:
        raise FlowConflict("not_a_queue_task", gettext("A tarefa já tem responsável."))
    task.assignee = actor
    task.save(update_fields={"assignee"})
    execution.log("task_claimed", task=task, step_key=task.step_key)
    execution.finish()
    return task


def release_task(task_id: Any, *, actor: Any, expected_revision: int) -> FlowTask:
    execution, task = _locked(task_id, actor)
    _ready(execution, task, actor, expected_revision)
    if task.assignee_id != actor.pk or (task.candidate_group_id is None and task.candidate_role_id is None):
        raise FlowConflict("not_claimed", gettext("Só uma tarefa de fila assumida pode ser liberada."))
    task.assignee = None
    task.save(update_fields={"assignee"})
    execution.log("task_released", task=task, step_key=task.step_key)
    execution.finish()
    return task


def reassign_task(task_id: Any, *, actor: Any, expected_revision: int, to_user_id: Any, comment: Any = "") -> FlowTask:
    """Hand a task to another person: its assignee when the step is delegable, or the administrator."""
    from .notifications import notify_task

    execution, task = _locked(task_id, actor)
    step = execution.spec.step(task.step_key)
    owner = task.assignee_id is not None and task.assignee_id == actor.pk
    if not (policies.is_flow_admin(actor) or (owner and step.delegable)):
        raise PermissionDenied("Only the assignee of a delegable task or the administrator reassigns it.")
    text = forms.clean_comment(comment)
    _ready(execution, task, actor, expected_revision, acting=False)
    try:
        people = active_people([to_user_id])
    except ValueError:
        people = []
    if not people:
        raise ValidationError({"to_user_id": gettext("Pessoa sem vínculo ativo nesta organização.")})
    if people[0] == task.assignee_id:
        raise ValidationError({"to_user_id": gettext("A tarefa já é dessa pessoa.")})
    previous = task.assignee_id
    task.delegated_from_id = previous or actor.pk
    task.assignee_id, task.candidate_group_id, task.candidate_role_id = people[0], None, None
    task.save(update_fields={"assignee", "delegated_from", "candidate_group", "candidate_role"})
    execution.log(
        "task_reassigned",
        task=task,
        step_key=task.step_key,
        detail={"from": str(previous) if previous else "", "to": str(people[0])},
        comment=text,
    )
    notify_task(execution, task, "flows.task_assigned")
    execution.finish()
    return task


def available_actions(execution: Execution, step: Step) -> list[Transition]:
    """The transitions a person may choose to complete a task now."""
    return [
        item for item in execution.spec.outgoing(step.key) if not item.on and execution.holds(item.condition, step.key)
    ]


def _advance(execution: Execution, action: Any) -> None:
    try:
        with transaction.atomic():
            action()
            execution.finish()
    except FlowFailure as failure:
        # A person's command changes nothing when the definition cannot go on.
        raise FlowConflict(
            "flow_failed", gettext("O fluxo não pôde avançar (%(code)s).") % {"code": failure.code}
        ) from None


def complete_task(
    task_id: Any, *, actor: Any, expected_revision: int, action: Any = None, data: Any = None, comment: Any = ""
) -> FlowTask:
    """Complete a task by one of its actions, with the form of the step and of the chosen transition."""
    execution, task = _locked(task_id, actor)
    _ready(execution, task, actor, expected_revision)
    if task.kind != "task":
        raise FlowConflict("not_a_task", gettext("Use a decisão para uma aprovação."))
    step = execution.spec.step(task.step_key)
    actions = available_actions(execution, step)
    if action in (None, "") and len(actions) == 1:
        chosen = actions[0]
    else:
        chosen = next((item for item in actions if item.ident == action), None)
    if chosen is None:
        raise ValidationError({"action": gettext("Escolha uma das ações disponíveis.")})
    values, sensitive = forms.clean((step.form, chosen.form), data)
    text = forms.clean_comment(comment)
    run = task.step_run

    def apply() -> None:
        execution.submit("task", values, sensitive, run=run, task=task, transition_key=chosen.ident)
        _close_task(
            task,
            "completed",
            assignee=actor,
            action=chosen.ident,
            comment=text,
            completed_by=actor,
            completed_at=execution.now,
        )
        execution.log("task_completed", run=run, task=task, detail={"action": chosen.ident}, comment=text)
        _cancel_open(execution, run, keep=task.pk)
        execution.complete(run, chosen.key or "completed", transition=chosen)

    _advance(execution, apply)
    return task


def _verdict(step: Step, run: FlowStepRun, decision: str) -> str:
    """``approved``, ``rejected`` or ``""`` (still open) for the tasks of an approval step."""
    tasks = list(FlowTask.objects.filter(step_run=run).exclude(status="cancelled").values_list("status", "decision"))
    approvals = sum(1 for _, value in tasks if value == "approved")
    rejections = sum(1 for _, value in tasks if value == "rejected")
    pending = sum(1 for status, _ in tasks if status == "open")
    if step.policy == "any":
        return decision
    if step.policy == "quorum":
        if approvals >= step.quorum:
            return "approved"
        return "rejected" if approvals + pending < step.quorum else ""
    if rejections:
        return "rejected"
    if step.policy == "sequential":
        return "approved" if int(run.state.get("index", 0)) + 1 >= len(run.state.get("slots", ())) else ""
    return "" if pending else "approved"


def decide_task(
    task_id: Any, *, actor: Any, expected_revision: int, decision: Any, data: Any = None, comment: Any = ""
) -> FlowTask:
    """Approve or reject as one approver; the step resolves when its policy is satisfied."""
    execution, task = _locked(task_id, actor)
    _ready(execution, task, actor, expected_revision)
    if task.kind != "approval":
        raise FlowConflict("not_an_approval", gettext("Esta tarefa é concluída por uma ação."))
    if decision not in DECISIONS:
        raise ValidationError({"decision": gettext("Use approved ou rejected.")})
    step = execution.spec.step(task.step_key)
    values, sensitive = forms.clean((step.form,), data)
    text = forms.clean_comment(comment)
    run = task.step_run

    def apply() -> None:
        execution.submit("decision", values, sensitive, run=run, task=task)
        _close_task(
            task,
            "completed",
            assignee=actor,
            decision=decision,
            comment=text,
            completed_by=actor,
            completed_at=execution.now,
        )
        execution.log("task_decided", run=run, task=task, detail={"decision": decision}, comment=text)
        verdict = _verdict(step, run, decision)
        if verdict:
            _cancel_open(execution, run, keep=task.pk)
            execution.complete(run, verdict)
        elif step.policy == "sequential":
            slots = [Slot.from_json(item) for item in run.state["slots"]]
            index = int(run.state.get("index", 0)) + 1
            run.state = {**run.state, "index": index}
            run.save(update_fields={"state"})
            _open_task(execution, run, step, slots[index], index)

    _advance(execution, apply)
    return task


# Deadlines.
def _due_task(task_id: str, kind: str) -> tuple[Execution, FlowTask, Step] | None:
    instance_id = FlowTask.objects.filter(pk=task_id).values_list("instance_id", flat=True).first()
    instance = lock_instance(instance_id) if instance_id else None
    if instance is None or instance.status != "running" or not app_enabled(instance.definition.app_label):
        return None
    task = FlowTask.objects.select_for_update().filter(pk=task_id, status="open").first()
    if task is None:
        return None
    execution = Execution(instance)
    step = execution.spec.step(task.step_key)
    return (execution, task, step) if step.sla else None


def remind_task(*, task_id: str, index: int) -> None:
    """Job handler: remind whoever holds the task, once per declared reminder."""
    from .notifications import notify_task

    found = _due_task(task_id, "reminder")
    if found is None:
        return
    execution, task, _ = found
    if task.reminded_count > index:
        return
    task.reminded_count = index + 1
    task.save(update_fields={"reminded_count"})
    execution.log("task_reminded", task=task, step_key=task.step_key, run=task.step_run)
    notify_task(execution, task, "flows.task_reminder")
    execution.finish()


def overdue_task(*, task_id: str, ticket: int) -> None:
    """Job handler at the due time: notify, hand over to the escalation or expire the step."""
    from .notifications import notify_slots, notify_task

    found = _due_task(task_id, "due")
    if found is None:
        return
    execution, task, step = found
    run = task.step_run
    if task.escalated_at is not None or int(run.state.get("sla_ticket", 0)) != ticket:
        return
    assert step.sla is not None  # noqa: S101 - type narrowing; only steps with an SLA are escalated
    try:
        with transaction.atomic():
            task.escalated_at = execution.now
            task.save(update_fields={"escalated_at"})
            execution.log("task_overdue", run=run, task=task, detail={"action": step.sla.on_overdue})
            if step.sla.on_overdue == "expire":
                _cancel_open(execution, run)
                execution.complete(run, "expired")
            else:
                slots = resolve(execution, step.key, step.sla.escalate_to)
                if step.sla.on_overdue == "reassign" and slots:
                    previous, slot = task.assignee_id, slots[0]
                    task.delegated_from_id = previous
                    task.assignee_id, task.candidate_group_id, task.candidate_role_id = slot.user, slot.group, slot.role
                    task.save(update_fields={"assignee", "delegated_from", "candidate_group", "candidate_role"})
                    execution.log(
                        "task_reassigned", run=run, task=task, detail={"to": slot.as_json(), "reason": "overdue"}
                    )
                    notify_task(execution, task, "flows.task_overdue")
                else:
                    notify_task(execution, task, "flows.task_overdue")
                    notify_slots(execution, task, "flows.task_overdue", slots)
            execution.save()
    except FlowFailure as failure:
        execution.instance.refresh_from_db()
        failed = Execution(execution.instance)
        failed.fail(failure.code, step_key=failure.step_key)
        failed.finish()
        return
    execution.finish()


def register_human_steps() -> None:
    from skeleton.jobs.registry import JobType, register_job_type

    behavior = Behavior(enter=_enter, cancel=_cancel, resume=_resume)
    register_behavior("task", behavior)
    register_behavior("approval", behavior)
    register_job_type(
        JobType(REMINDER_JOB, remind_task, {"task_id": "uuid", "index": "int"}, run_as="system", owner_app="sk_flows")
    )
    register_job_type(
        JobType(DUE_JOB, overdue_task, {"task_id": "uuid", "ticket": "int"}, run_as="system", owner_app="sk_flows")
    )
