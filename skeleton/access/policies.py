from __future__ import annotations

from collections.abc import Iterable
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


class _Subject:
    """The actor's membership, resolved once for ONE policy decision (#227).

    Never store an instance beyond the call that created it (no ContextVar, request
    attribute, module cache): authorization is recomputed on every decision.
    """

    __slots__ = ("user", "membership", "_admin", "_tables")

    def __init__(self, user: User) -> None:
        self.user = user
        self.membership = active_membership(user)  # The only Membership query of the decision.
        self._admin: bool | None = None
        self._tables: dict[str, TableDefinition | None] = {}

    def group_ids(self) -> QuerySet[Any, UUID]:
        from .models import GroupMember

        return GroupMember.objects.filter(membership=self.membership, group__deleted_at__isnull=True).values_list(
            "group_id", flat=True
        )

    def roles(self) -> QuerySet[Role]:
        from .models import GroupRole, MemberRole, Role

        direct = MemberRole.objects.filter(membership=self.membership, role__deleted_at__isnull=True).values_list(
            "role_id", flat=True
        )
        grouped = GroupRole.objects.filter(group_id__in=self.group_ids(), role__deleted_at__isnull=True).values_list(
            "role_id", flat=True
        )
        return Role.objects.filter(Q(sysid__in=direct) | Q(sysid__in=grouped))

    def is_admin(self) -> bool:
        if self._admin is None:
            self._admin = self.roles().filter(code="organization_admin", is_builtin=True).exists()
        return self._admin

    def table(self, model: type[Model]) -> TableDefinition | None:
        label = model._meta.label_lower
        if label not in self._tables:
            self._tables[label] = table_definition(model)
        return self._tables[label]


def group_ids(user: User) -> QuerySet[Any, UUID]:
    return _Subject(user).group_ids()


def effective_roles(user: User) -> QuerySet[Role]:
    return _Subject(user).roles()


def is_organization_admin(user: User) -> bool:
    try:
        return _Subject(user).is_admin()
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

    if action not in ACTIONS or not _scope_allows(model, action):
        return False
    return _permits(_Subject(user), model, action)


def _scope_allows(model: type[Model], action: str) -> bool:
    # The single point where a token scope applies: it narrows the role, never widens it.
    scopes = credential_scopes()
    return scopes is None or f"{model._meta.label_lower}:{action}" in scopes


def _permits(subject: _Subject, model: type[Model], action: str) -> bool:
    from .models import TablePermission

    table = subject.table(model)
    return (
        table is not None
        and TablePermission.objects.filter(table=table, role__in=subject.roles(), action=action).exists()
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
    return _grants(_Subject(user), model, action)


def _grants(subject: _Subject, model: type[Model], action: str) -> QuerySet[Any, UUID]:
    from .models import RecordGrant

    return (
        RecordGrant.objects.filter(table=subject.table(model), action=action)
        .filter(Q(membership=subject.membership) | Q(group_id__in=subject.group_ids()) | Q(role__in=subject.roles()))
        .values_list("record_sysid", flat=True)
    )


def _authorized[RecordT: SecuredRecord](
    model: type[RecordT], subject: _Subject, action: str, include_deleted: bool
) -> QuerySet[RecordT]:
    """The visibility rule over an already permitted table (one place for the rule)."""
    queryset = model.objects.with_deleted() if include_deleted else model.objects.all()
    if subject.is_admin():
        return queryset
    visibility = Q(owner_id=subject.user.pk)
    if action == "read":
        visibility |= Q(visibility="organization") | Q(visibility="group", owning_group_id__in=subject.group_ids())
    if action in ("read", "update", "delete"):
        visibility |= Q(sysid__in=_grants(subject, model, action))
    return queryset.filter(visibility)


def authorized_queryset[RecordT: SecuredRecord](
    model: type[RecordT], *, user: User, action: str = "read", include_deleted: bool = False
) -> QuerySet[RecordT]:
    from skeleton.common.models import SecuredRecord

    spec = get_spec(model._meta.label_lower)
    if not issubclass(model, SecuredRecord) or not spec.expose_api:
        raise PermissionDenied("This table is not a business resource.")
    from skeleton.system.applications import require_application_enabled

    require_application_enabled(model._meta.app_label)
    if include_deleted and action != "restore":
        # Reading a tombstone also requires the restore authorization (docs/access-control.md, #229).
        raise PermissionDenied("Deleted records require the restore action.")
    if action not in ACTIONS or not _scope_allows(model, action):
        raise PermissionDenied("Table permission required.")
    subject = _Subject(user)
    if not _permits(subject, model, action):
        raise PermissionDenied("Table permission required.")
    return _authorized(model, subject, action, include_deleted)


def require_record_action[RecordT: SecuredRecord](record: RecordT, *, user: User, action: str) -> RecordT:
    if record.deleted_at is not None and action != "restore":
        raise PermissionDenied("Deleted records require the restore action.")
    if (
        not authorized_queryset(type(record), user=user, action=action, include_deleted=record.deleted_at is not None)
        .filter(sysid=record.pk)
        .exists()
    ):
        raise PermissionDenied("Record permission required.")
    return record


def users_who_can_read(record: SecuredRecord, user_ids: Iterable[UUID]) -> set[UUID]:
    """The subset of ``user_ids`` for whom ``require_record_action(record, user=..., action="read")`` passes.

    The same layers as ``authorized_queryset``, decided in memory from a fixed number of
    ``IN`` queries per call, never one authorization per person (#296): active membership of
    an active user, effective roles (direct and through live groups), the table's read
    permission, then the organization administrator, the owner, the visibility and the read
    grants. Requires a tenant context; an unexposed table, a disabled app, a token without
    the table's read scope or a deleted record give the empty set. Never raises
    ``PermissionDenied``.
    """
    from skeleton.common.models import SecuredRecord
    from skeleton.system.applications import require_application_enabled
    from skeleton.tenancy.models import Membership

    from .models import GroupMember, GroupRole, MemberRole, RecordGrant, Role, TablePermission

    if current_context().organization_id is None:
        raise MissingTenantContext("Authorization requires a tenant context.")
    wanted = set(user_ids)
    model = type(record)
    if not wanted or record.deleted_at is not None or not issubclass(model, SecuredRecord):
        return set()
    try:
        spec = get_spec(model._meta.label_lower)
    except KeyError:
        return set()
    if not spec.expose_api or not _scope_allows(model, "read"):
        return set()
    try:
        require_application_enabled(model._meta.app_label)
    except PermissionDenied:  # ApplicationUnavailable included.
        return set()
    table = table_definition(model)
    if table is None:
        return set()
    owners = dict(
        Membership.objects.filter(
            user_id__in=wanted,
            is_active=True,
            organization__is_active=True,
            organization__deleted_at__isnull=True,
            user__is_active=True,
            user__deleted_at__isnull=True,
        ).values_list("pk", "user_id")
    )
    if not owners:
        return set()
    groups: dict[UUID, set[UUID]] = {membership: set() for membership in owners}
    for membership, group in GroupMember.objects.filter(
        membership_id__in=owners, group__deleted_at__isnull=True
    ).values_list("membership_id", "group_id"):
        groups[membership].add(group)
    roles: dict[UUID, set[UUID]] = {membership: set() for membership in owners}
    for membership, role in MemberRole.objects.filter(
        membership_id__in=owners, role__deleted_at__isnull=True
    ).values_list("membership_id", "role_id"):
        roles[membership].add(role)
    every_group = set().union(*groups.values())
    by_group: dict[UUID, set[UUID]] = {}
    for group, role in GroupRole.objects.filter(group_id__in=every_group, role__deleted_at__isnull=True).values_list(
        "group_id", "role_id"
    ):
        by_group.setdefault(group, set()).add(role)
    for membership, member_groups in groups.items():
        for group in member_groups:
            roles[membership] |= by_group.get(group, set())
    every_role = set().union(*roles.values())
    live_roles = set(Role.objects.filter(pk__in=every_role).values_list("pk", flat=True))
    readers = set(
        TablePermission.objects.filter(table=table, role_id__in=live_roles, action="read").values_list(
            "role_id", flat=True
        )
    )
    admins = set(
        Role.objects.filter(pk__in=live_roles, code="organization_admin", is_builtin=True).values_list("pk", flat=True)
    )
    grants = list(
        RecordGrant.objects.filter(table=table, action="read", record_sysid=record.pk).values_list(
            "membership_id", "group_id", "role_id"
        )
    )
    allowed = set()
    for membership, user_id in owners.items():
        effective = roles[membership] & live_roles
        if not effective & readers:
            continue  # TablePermission is required before anything else.
        member_groups = groups[membership]
        if (
            effective & admins
            or record.owner_id == user_id
            or record.visibility == "organization"
            or (record.visibility == "group" and record.owning_group_id in member_groups)
            or any(
                grant_membership == membership or grant_group in member_groups or grant_role in effective
                for grant_membership, grant_group, grant_role in grants
            )
        ):
            allowed.add(user_id)
    return allowed


def authorized_audit_queryset(user: User) -> QuerySet[AuditEvent]:
    from skeleton.audit.models import AuditEvent
    from skeleton.system.registry import all_specs

    organization_id = current_context().organization_id
    if organization_id is None:
        from skeleton.accounts.platform import AUDIT_READ, has_platform_capability

        if is_platform_admin(user) or has_platform_capability(user, AUDIT_READ):
            return AuditEvent.objects.filter(organization_id__isnull=True)
        raise PermissionDenied("Platform access required.")
    from skeleton.common.models import SecuredRecord
    from skeleton.system.applications import require_application_enabled

    from .models import TablePermission

    subject = _Subject(user)
    events = AuditEvent.objects.filter(organization_id=organization_id)
    if subject.is_admin():
        return events
    # The actor's audit/read/restore permissions in one query, decided in memory per table (#227).
    permitted = set(
        TablePermission.objects.filter(
            role__in=subject.roles(),
            action__in=("audit", "read", "restore"),
            table__available=True,
            table__deleted_at__isnull=True,
        ).values_list("table__model_label", "action")
    )

    def allows(model: type[Model], action: str) -> bool:
        return (model._meta.label_lower, action) in permitted and _scope_allows(model, action)

    allowed = Q(pk__in=[])
    for spec in all_specs():
        model = spec.model
        if not spec.expose_api or not issubclass(model, SecuredRecord) or not allows(model, "audit"):
            continue
        try:
            require_application_enabled(model._meta.app_label)
        except PermissionDenied:
            continue  # A disabled app hides only its own tables.
        if not allows(model, "read"):
            continue
        label = model._meta.label_lower
        live = _authorized(model, subject, "read", False).values_list("sysid", flat=True)
        condition = Q(model_label=label, record_sysid__in=live)
        if allows(model, "restore"):
            restored = _authorized(model, subject, "restore", True).filter(deleted_at__isnull=False)
            condition |= Q(model_label=label, record_sysid__in=restored.values_list("sysid", flat=True))
        allowed |= condition
    return events.filter(allowed)
