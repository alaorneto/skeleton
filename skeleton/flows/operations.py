"""Operations panel of an organization's flows (#408): what needs action now.

Only who administers flows (the organization administrator or ``flows.manage``) reads it. The
panel counts failed instances and open tasks without an assignee, overdue or due within a day,
and lists the most recent of each, in a fixed number of queries whatever the number of
instances. It carries identifiers, states and dates only: never form values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.utils import timezone

LIST_LIMIT = 50
DUE_SOON = timedelta(hours=24)


@dataclass(frozen=True)
class OperationsCounts:
    failed: int
    unassigned: int
    overdue: int
    due_soon: int


@dataclass(frozen=True)
class OperationsInstance:
    sysid: UUID
    flow_key: str
    flow_version: int
    number: str
    status: str
    updated_at: datetime


@dataclass(frozen=True)
class OperationsTask:
    sysid: UUID
    instance_id: UUID
    flow_key: str
    step_key: str
    kind: str
    assignee_id: UUID | None
    due_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class OperationsOverview:
    counts: OperationsCounts
    failed: tuple[OperationsInstance, ...]
    unassigned: tuple[OperationsTask, ...]
    overdue: tuple[OperationsTask, ...]
    due_soon: tuple[OperationsTask, ...]
    flow_keys: tuple[str, ...]


def _instance(row: Any) -> OperationsInstance:
    return OperationsInstance(row.pk, row.flow_key, row.flow_version, row.number, row.status, row.updated_at)


def _task(row: Any) -> OperationsTask:
    return OperationsTask(
        row.pk,
        row.instance_id,
        row.instance.flow_key,
        row.step_key,
        row.kind,
        row.assignee_id,
        row.due_at,
        row.created_at,
    )


def overview(
    user: Any, *, flow_key: str = "", flow_version: int | None = None, now: datetime | None = None
) -> OperationsOverview:
    """The panel of the current organization; ``PermissionDenied`` for anyone who does not administer flows."""
    from . import policies

    if not policies.is_flow_admin(user):
        raise PermissionDenied("Flow administration required.")
    now = now or timezone.now()
    instances = policies.visible_instances(user)
    tasks = policies.visible_tasks(user).filter(status="open")
    flow_keys = tuple(sorted(set(instances.order_by().values_list("flow_key", flat=True).distinct())))
    if flow_key:
        instances = instances.filter(flow_key=flow_key)
        tasks = tasks.filter(instance__flow_key=flow_key)
    if flow_version is not None:
        instances = instances.filter(flow_version=flow_version)
        tasks = tasks.filter(instance__flow_version=flow_version)
    unassigned = Q(assignee__isnull=True)
    overdue = Q(due_at__lt=now)
    due_soon = Q(due_at__gte=now, due_at__lt=now + DUE_SOON)
    totals = tasks.aggregate(
        unassigned=Count("pk", filter=unassigned),
        overdue=Count("pk", filter=overdue),
        due_soon=Count("pk", filter=due_soon),
    )
    failed = instances.filter(status="failed")
    rows = tasks.select_related("instance")

    def listing(condition: Q, order: str) -> tuple[OperationsTask, ...]:
        return tuple(_task(row) for row in rows.filter(condition).order_by(order, "sysid")[:LIST_LIMIT])

    return OperationsOverview(
        counts=OperationsCounts(failed=failed.count(), **totals),
        failed=tuple(_instance(row) for row in failed.order_by("-updated_at", "sysid")[:LIST_LIMIT]),
        unassigned=listing(unassigned, "created_at"),
        overdue=listing(overdue, "due_at"),
        due_soon=listing(due_soon, "due_at"),
        flow_keys=flow_keys,
    )
