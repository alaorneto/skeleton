"""Open tasks of a person across organizations (#409), by self-discovery on the assignee.

Only tasks assigned to the person qualify: a queue task is decided per organization (groups and
roles of that tenant), so it stays in the organization's inbox. The rows carry only task
columns (no join with the instance, which another tenant's RLS hides), limited to the
organizations where the person keeps an active membership.
"""

from __future__ import annotations

from typing import Any

from skeleton.tenancy.context import memberships_for_user, own_rows

LIMIT = 200


def _open(user: Any, organization_ids: list[Any]) -> Any:
    from .models import FlowTask

    return own_rows(FlowTask, user).filter(status="open", organization_id__in=organization_ids)


def open_task_count(user: Any, *, organization_ids: list[Any] | None = None) -> int:
    if not getattr(user, "is_authenticated", False):
        return 0
    if organization_ids is None:
        organization_ids = [row["organization_id"] for row in memberships_for_user(user)]
    return _open(user, list(organization_ids)).count() if organization_ids else 0


def open_tasks(user: Any) -> list[dict[str, Any]]:
    """The person's open tasks, most urgent first, with the organization's name."""
    memberships = memberships_for_user(user)
    names = {row["organization_id"]: row["organization_name"] for row in memberships}
    if not names:
        return []
    rows = _open(user, list(names)).order_by("due_at", "created_at", "sysid")[:LIMIT]
    return [
        {
            "sysid": row.pk,
            "organization_id": row.organization_id,
            "organization_name": names[row.organization_id],
            "step_key": row.step_key,
            "kind": row.kind,
            "due_at": row.due_at,
            "created_at": row.created_at,
        }
        for row in rows
    ]
