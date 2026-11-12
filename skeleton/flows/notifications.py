"""Notifications of flow tasks: assigned, reminder and overdue (docs/flows.md, "Integrações").

The types have no target table: the fan-out re-authorizes a target through the record
policies of exposed ``SecuredRecord`` tables, and an approver may not read the subject
record. The payload carries labels of the definition and identifiers only, never form
data, and opening the notification re-authorizes the access to the task.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ValidationError

TYPES = {
    "flows.task_assigned": "Tarefa de fluxo atribuída",
    "flows.task_reminder": "Lembrete de tarefa de fluxo",
    "flows.task_overdue": "Tarefa de fluxo atrasada",
}
PAYLOAD = ("flow", "step", "number", "task")


def task_url(user: Any, row: Any) -> str | None:
    """Where a task notification leads, only if the person may still read the task."""
    from django.urls import NoReverseMatch, reverse

    from . import policies
    from .errors import FlowUnavailable

    task_id = row.payload.get("task")
    try:
        if not task_id or not policies.visible_tasks(user).filter(pk=task_id).exists():
            return None
        return reverse("flows:task", kwargs={"organization_id": row.organization_id, "sysid": task_id})
    except (FlowUnavailable, NoReverseMatch, ValidationError, ValueError):
        return None


def register_flow_notification_types() -> None:
    from skeleton.notifications.registry import NotificationType, register_notification_type

    for code, label in TYPES.items():
        register_notification_type(
            NotificationType(
                code,
                label,
                payload_fields=PAYLOAD,
                group_key="flow-task:{task}",
                owner_app="sk_flows",
                open_url=task_url,
            )
        )


def _send(
    execution: Any,
    task: Any,
    code: str,
    *,
    users: Iterable[Any] = (),
    groups: Iterable[Any] = (),
    roles: Iterable[Any] = (),
) -> None:
    from skeleton.notifications.services import Recipients, notify

    recipients = Recipients(tuple(users), tuple(groups), tuple(roles))
    if not (recipients.users or recipients.groups or recipients.roles):
        return
    step = execution.spec.step(task.step_key)
    try:
        notify(
            code,
            recipients=recipients,
            payload={
                "flow": execution.spec.label,
                "step": step.title,
                "number": execution.instance.number,
                "task": str(task.pk),
            },
        )
    except ValidationError:
        # A recipient left the organization meanwhile: the task stays, only the notice is skipped.
        return


def notify_task(execution: Any, task: Any, code: str) -> None:
    """Notify whoever holds the task now: its assignee, or the queue of its group or role."""
    _send(
        execution,
        task,
        code,
        users=[task.assignee_id] if task.assignee_id else (),
        groups=[task.candidate_group_id] if task.candidate_group_id and not task.assignee_id else (),
        roles=[task.candidate_role_id] if task.candidate_role_id and not task.assignee_id else (),
    )


def notify_slots(execution: Any, task: Any, code: str, slots: Iterable[Any]) -> None:
    slots = list(slots)
    _send(
        execution,
        task,
        code,
        users=[slot.user for slot in slots if slot.user],
        groups=[slot.group for slot in slots if slot.group],
        roles=[slot.role for slot in slots if slot.role],
    )
