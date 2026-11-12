from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.db.models import Model, Q, QuerySet

from skeleton.accounts.platform import is_platform_admin
from skeleton.system.registry import get_spec
from skeleton.tenancy.context import MissingTenantContext, current_context

if TYPE_CHECKING:
    from skeleton.accounts.models import User
    from skeleton.audit.models import AuditEvent
    from skeleton.common.models import SecuredRecord
    from skeleton.system.models import TableDefinition
    from skeleton.tenancy.models import Membership

    from .models import Role

ACTIONS = {"read", "create", "update", "delete", "restore", "manage_access", "audit"}


def active_membership(user: User) -> Membership:
    from skeleton.tenancy.models import Membership

    context = current_context()
    if context.organization_id is None:
        raise MissingTenantContext("Authorization requires a tenant context.")
    if not getattr(user, "is_authenticated", False) or not user.is_active or user.deleted_at is not None:
        raise PermissionDenied("Active membership required.")
    membership = Membership.objects.filter(
        user=user, is_active=True, organization__is_active=True, organization__deleted_at__isnull=True
    ).first()
    if membership is None:
        raise PermissionDenied("Active membership required.")
    return membership


def group_ids(user: User) -> QuerySet[Any, UUID]:
    from .models import GroupMember

    membership = active_membership(user)
    return GroupMember.objects.filter(membership=membership, group__deleted_at__isnull=True).values_list(
        "group_id", flat=True
    )


def effective_roles(user: User) -> QuerySet[Role]:
    from .models import GroupRole, MemberRole, Role

    membership = active_membership(user)
    direct = MemberRole.objects.filter(membership=membership, role__deleted_at__isnull=True).values_list(
        "role_id", flat=True
    )
    grouped = GroupRole.objects.filter(group_id__in=group_ids(user), role__deleted_at__isnull=True).values_list(
        "role_id", flat=True
    )
    return Role.objects.filter(Q(sysid__in=direct) | Q(sysid__in=grouped))


def is_organization_admin(user: User) -> bool:
    try:
        return effective_roles(user).filter(code="organization_admin", is_builtin=True).exists()
    except PermissionDenied:
        return False


def table_definition(model: type[Model]) -> TableDefinition | None:
    from skeleton.system.models import TableDefinition

    return TableDefinition.objects.filter(model_label=model._meta.label_lower, available=True).first()


def credential_scopes() -> frozenset[str] | None:
    """Scopes of the API token of this request, or ``None`` for a session (#46)."""
    from skeleton.tenancy.context import current_context

    return current_context().credential_scopes


def require_credential_scope(scope: str) -> None:
    """A token must carry ``scope``; a session passes. The only other place scopes apply."""
    scopes = credential_scopes()
    if scopes is not None and scope not in scopes:
        raise PermissionDenied("Token scope required.")


def has_table_permission(model: type[Model], *, user: User, action: str) -> bool:
    from .models import TablePermission

    if action not in ACTIONS:
        return False
    scopes = credential_scopes()
    if scopes is not None and f"{model._meta.label_lower}:{action}" not in scopes:
        # The single point where a token scope applies: it narrows the role, never widens it.
        return False
    active_membership(user)
    table = table_definition(model)
    return (
        table is not None
        and TablePermission.objects.filter(table=table, role__in=effective_roles(user), action=action).exists()
    )


def table_permissions(user: User) -> frozenset[tuple[str, str]]:
    """``(model_label, action)`` pairs the user's roles hold in the tenant, in one query.

    The batch form of ``has_table_permission`` for menus: the same roles, catalog
    availability and token scopes. Without an active membership the set is empty.
    """
    from .models import TablePermission

    try:
        rows = TablePermission.objects.filter(
            role__in=effective_roles(user), table__available=True, action__in=ACTIONS
        ).values_list("table__model_label", "action")
        pairs = frozenset(rows)
    except PermissionDenied:
        return frozenset()
    scopes = credential_scopes()
    if scopes is not None:
        pairs = frozenset(pair for pair in pairs if f"{pair[0]}:{pair[1]}" in scopes)
    return pairs


def _grant_ids(model: type[Model], user: User, action: str) -> QuerySet[Any, UUID]:
    from .models import RecordGrant

    membership = active_membership(user)
    return (
        RecordGrant.objects.filter(table=table_definition(model), action=action)
        .filter(Q(membership=membership) | Q(group_id__in=group_ids(user)) | Q(role__in=effective_roles(user)))
        .values_list("record_sysid", flat=True)
    )


def authorized_queryset[RecordT: SecuredRecord](
    model: type[RecordT], *, user: User, action: str = "read", include_deleted: bool = False
) -> QuerySet[RecordT]:
    from skeleton.common.models import SecuredRecord

    spec = get_spec(model._meta.label_lower)
    if not issubclass(model, SecuredRecord) or not spec.expose_api:
        raise PermissionDenied("This table is not a business resource.")
    from skeleton.system.applications import require_application_enabled

    require_application_enabled(model._meta.app_label)
    if not has_table_permission(model, user=user, action=action):
        raise PermissionDenied("Table permission required.")
    queryset = model.objects.with_deleted() if include_deleted else model.objects.all()
    if is_organization_admin(user):
        return queryset
    visibility = Q(owner_id=user.pk)
    if action == "read":
        visibility |= Q(visibility="organization") | Q(visibility="group", owning_group_id__in=group_ids(user))
    if action in ("read", "update", "delete"):
        visibility |= Q(sysid__in=_grant_ids(model, user, action))
    return queryset.filter(visibility)


def require_record_action[RecordT: SecuredRecord](record: RecordT, *, user: User, action: str) -> RecordT:
    if (
        not authorized_queryset(type(record), user=user, action=action, include_deleted=record.deleted_at is not None)
        .filter(sysid=record.pk)
        .exists()
    ):
        raise PermissionDenied("Record permission required.")
    return record


def authorized_audit_queryset(user: User) -> QuerySet[AuditEvent]:
    from skeleton.audit.models import AuditEvent
    from skeleton.system.registry import all_specs

    organization_id = current_context().organization_id
    if organization_id is None:
        from skeleton.accounts.platform import AUDIT_READ, has_platform_capability

        if is_platform_admin(user) or has_platform_capability(user, AUDIT_READ):
            return AuditEvent.objects.filter(organization_id__isnull=True)
        raise PermissionDenied("Platform access required.")
    active_membership(user)
    events = AuditEvent.objects.filter(organization_id=organization_id)
    if is_organization_admin(user):
        return events
    allowed = Q(pk__in=[])
    for spec in all_specs():
        if not spec.expose_api or not has_table_permission(spec.model, user=user, action="audit"):
            continue
        try:
            live = authorized_queryset(spec.model, user=user).values_list("sysid", flat=True)
        except PermissionDenied:
            continue
        condition = Q(model_label=spec.model._meta.label_lower, record_sysid__in=live)
        if has_table_permission(spec.model, user=user, action="restore"):
            restored = (
                authorized_queryset(spec.model, user=user, action="restore", include_deleted=True)
                .filter(deleted_at__isnull=False)
                .values_list("sysid", flat=True)
            )
            condition |= Q(model_label=spec.model._meta.label_lower, record_sysid__in=restored)
        allowed |= condition
    return events.filter(allowed)
