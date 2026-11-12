"""Who may see and act on flows (docs/flows.md, "Autorização"): default deny, revalidated on every call.

The flow tables have no ``TablePermission``. Reading an instance derives from reading its
subject record or from taking part in it; acting on a task derives from its assignment, as
it is **now** (no cache: a revoked role loses the queue on the next call). A disabled owner
app hides and blocks everything of its flows.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import Q, QuerySet

from .errors import FlowUnavailable
from .models import FlowDefinition, FlowInstance, FlowTask


def require_flow_app(app_label: str) -> None:
    """The owner app of a flow must be installed, available and enabled in the organization."""
    from skeleton.system.app_registry import get_app
    from skeleton.system.applications import require_application_enabled

    try:
        get_app(app_label)
        require_application_enabled(app_label)
    except (KeyError, PermissionDenied):
        raise FlowUnavailable() from None


def enabled_flow_apps() -> list[str]:
    """Owner apps of the organization's definitions that are enabled right now."""
    labels = FlowDefinition.objects.order_by().values_list("app_label", flat=True).distinct()
    enabled = []
    for label in labels:
        try:
            require_flow_app(label)
        except FlowUnavailable:
            continue
        enabled.append(label)
    return enabled


def is_flow_admin(user: Any) -> bool:
    """Who administers flows: the organization administrator (docs/flows.md, decision 5)."""
    from skeleton.access.policies import is_organization_admin

    return user is not None and is_organization_admin(user)


def has_membership(user: Any) -> bool:
    from skeleton.access.policies import active_membership

    try:
        active_membership(user)
    except PermissionDenied:
        return False
    return True


def queue_filter(user: Any) -> Q:
    """Open queue tasks the person is a candidate for, by current group membership and roles."""
    from skeleton.access.policies import effective_roles, group_ids

    return Q(status="open", assignee__isnull=True) & (
        Q(candidate_group_id__in=group_ids(user)) | Q(candidate_role__in=effective_roles(user))
    )


def participation_filter(user: Any) -> Q:
    """Tasks that make the person part of an instance: assigned, done, delegated or in their queue."""
    return Q(assignee=user) | Q(completed_by=user) | Q(delegated_from=user) | queue_filter(user)


def _subject_filter(user: Any) -> Q:
    from skeleton.access.policies import authorized_queryset
    from skeleton.system.registry import get_spec

    condition = Q(pk__in=[])
    tables = FlowInstance.objects.order_by().values_list("subject_table", flat=True).distinct()
    for table_key in tables:
        try:
            readable = authorized_queryset(get_spec(table_key).model, user=user, action="read")
        except (KeyError, PermissionDenied):
            continue
        condition |= Q(subject_table=table_key, subject_sysid__in=readable.values("sysid"))
    return condition


def visible_instances(user: Any) -> QuerySet[FlowInstance]:
    """Instances the person may read, of enabled apps only; filter before counting or paginating."""
    if not has_membership(user):
        return FlowInstance.objects.none()
    rows = FlowInstance.objects.filter(definition__app_label__in=enabled_flow_apps())
    if is_flow_admin(user):
        return rows
    taking_part = FlowTask.objects.filter(participation_filter(user)).values("instance_id")
    return rows.filter(Q(starter=user) | Q(pk__in=taking_part) | _subject_filter(user))


def actionable_tasks(user: Any) -> QuerySet[FlowTask]:
    """Open tasks the person may act on now: their own and the queues they belong to."""
    if not has_membership(user):
        return FlowTask.objects.none()
    return FlowTask.objects.filter(
        Q(status="open") & (Q(assignee=user) | queue_filter(user)),
        instance__definition__app_label__in=enabled_flow_apps(),
    )


def visible_tasks(user: Any) -> QuerySet[FlowTask]:
    """Tasks the person may read: the actionable ones, their past ones and, for the administrator, all."""
    if not has_membership(user):
        return FlowTask.objects.none()
    rows = FlowTask.objects.filter(instance__definition__app_label__in=enabled_flow_apps())
    if is_flow_admin(user):
        return rows
    return rows.filter(participation_filter(user))
