"""All enterprise access changes are serialized on their organization row."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.tenancy.context import current_context

from .capabilities import (
    ACCESS_MANAGE,
    BUILTIN_CODES,
    BUILTIN_ROLES,
    MEMBERS_MANAGE,
    has_capability,
    membership_holds_capability,
    require_capability,
)
from .policies import is_organization_admin

if TYPE_CHECKING:
    from uuid import UUID

    from django.db.models import Model, QuerySet

    from skeleton.accounts.models import User
    from skeleton.system.models import TableDefinition
    from skeleton.system.registry import TableSpec
    from skeleton.tenancy.models import Membership, Organization

    from .models import AccessGroup, GroupMember, GroupRole, MemberRole, RecordGrant, Role, TablePermission


class LastAdministrator(ValidationError):
    pass


def require_organization_admin(user: User) -> None:
    from .policies import credential_scopes

    if credential_scopes() is not None or not is_organization_admin(user):
        raise PermissionDenied("Organization administrator required.")


@contextmanager
def organization_lock(user: User | None = None, *, capability: str | None = None) -> Iterator[Organization]:
    """Lock the organization before its records; HTTP callers must pass the actor.

    Without ``capability`` the actor must be the organization administrator;
    with it, the actor needs that capability of a builtin role.
    """
    from skeleton.tenancy.models import Organization

    context = current_context()
    if context.organization_id is None:
        raise PermissionDenied("Organization required.")
    with transaction.atomic(using=context.using):
        organization = (
            Organization.all_objects.using(context.using).select_for_update(no_key=True).get(pk=context.organization_id)
        )
        if not organization.is_active or organization.deleted_at is not None:
            raise PermissionDenied("Organization unavailable.")
        if user is not None:
            if capability is None:
                require_organization_admin(user)
            else:
                require_capability(user, capability)
        yield organization


def require_unprivileged_target(
    user: User | None, *, role: Role | None = None, membership: Membership | None = None
) -> None:
    """Delegated managers cannot hand out or remove administrative capabilities.

    Only a holder of ``access.manage`` (the organization administrator) may
    assign a capability role or deactivate someone who holds one.
    """
    from .capabilities import privileged_role

    if user is None or has_capability(user, ACCESS_MANAGE):
        return
    if role is not None and privileged_role(role):
        raise PermissionDenied("Only an administrator can assign a role with capabilities.")
    if membership is not None and membership_holds_capability(membership):
        raise PermissionDenied("Only an administrator can change someone who holds capabilities.")


def administrator_memberships() -> QuerySet[Membership]:
    """Active memberships of the current tenant that hold ``organization_admin``, directly or by group."""
    from django.db.models import Q

    from skeleton.tenancy.models import Membership

    from .models import GroupMember, GroupRole, MemberRole

    direct = MemberRole.objects.filter(
        role__code="organization_admin", role__is_builtin=True, role__deleted_at__isnull=True
    ).values_list("membership_id", flat=True)
    groups = GroupRole.objects.filter(
        role__code="organization_admin",
        role__is_builtin=True,
        role__deleted_at__isnull=True,
        group__deleted_at__isnull=True,
    ).values_list("group_id", flat=True)
    indirect = GroupMember.objects.filter(group_id__in=groups).values_list("membership_id", flat=True)
    return Membership.objects.filter(
        Q(pk__in=direct) | Q(pk__in=indirect), is_active=True, user__is_active=True, user__deleted_at__isnull=True
    )


def require_service_account_manager(user: User | None, membership: Membership) -> None:
    """Only ``integrations.manage`` changes a service account's membership; a people manager cannot (#44)."""
    from .capabilities import INTEGRATIONS_MANAGE

    if user is not None and membership.user.is_service_account:
        require_capability(user, INTEGRATIONS_MANAGE)


def ensure_last_admin() -> None:
    if not administrator_memberships().exists():
        raise LastAdministrator("An organization must retain an active administrator.")


def _tenant_relation(obj: Any) -> None:
    if (
        getattr(obj, "organization_id", current_context().organization_id) != current_context().organization_id
        or obj.deleted_at is not None
    ):
        raise ValidationError(gettext("A relação precisa estar ativa e pertencer à organização atual."))
    if hasattr(obj, "is_active") and not obj.is_active:
        raise ValidationError(gettext("A relação precisa estar ativa."))


def _create[R: Model](
    model: type[R],
    *,
    user: User | None,
    data: Mapping[str, Any],
    fields: Iterable[str],
    capability: str = ACCESS_MANAGE,
) -> R:
    if set(data) - set(fields):
        raise ValidationError(gettext("Campos não permitidos."))
    with organization_lock(user, capability=capability) as organization:
        for value in data.values():
            if hasattr(value, "_meta"):
                _tenant_relation(value)
        record = model(organization=organization, **data)
        record.save()
        return record


def _require_type(record: object, allowed: tuple[type[Model], ...]) -> None:
    """Each public service manages one kind of record; another kind is a caller bug (#268)."""
    if not isinstance(record, allowed):
        raise TypeError(f"{type(record).__name__} is not managed by this service.")


def _roles() -> tuple[type[Role]]:
    from .models import Role

    return (Role,)


def _groups() -> tuple[type[AccessGroup]]:
    from .models import AccessGroup

    return (AccessGroup,)


def _links() -> tuple[type[Model], ...]:
    from .models import GroupMember, GroupRole, MemberRole, RecordGrant, TablePermission

    return (MemberRole, GroupMember, GroupRole, TablePermission, RecordGrant)


def _update(
    record: Any,
    *,
    user: User | None,
    data: Mapping[str, Any],
    fields: Iterable[str],
    allowed: tuple[type[Model], ...],
    capability: str = ACCESS_MANAGE,
) -> Any:
    _require_type(record, allowed)
    if set(data) - set(fields):
        raise ValidationError(gettext("Campos não permitidos."))
    with organization_lock(user, capability=capability):
        _tenant_relation(record)
        if getattr(record, "is_builtin", False) and "code" in data and data["code"] != record.code:
            raise ValidationError(gettext("O código de um papel interno não muda."))
        for key, value in data.items():
            setattr(record, key, value)
        record.save(update_fields=set(data))
        ensure_last_admin()
        return record


def _delete(
    record: Any, *, user: User | None, allowed: tuple[type[Model], ...], capability: str = ACCESS_MANAGE
) -> None:
    _require_type(record, allowed)
    with organization_lock(user, capability=capability):
        _tenant_relation(record)
        if getattr(record, "is_builtin", False):
            raise ValidationError(gettext("Papéis internos não podem ser removidos."))
        record.delete()
        ensure_last_admin()


def provision_membership(*, organization: Organization, user: User, role: Role) -> Membership:
    context = current_context()
    if not context.system or context.organization_id != organization.pk:
        raise PermissionDenied("Provisioning requires a validated system tenant context.")
    with organization_lock():
        return admit_member(organization=organization, user=user, role=role)


def admit_member(*, organization: Organization, user: User, role: Role) -> Membership:
    """Create or reactivate ``user``'s membership with ``role`` in the current organization.

    The caller has already decided and holds ``organization_lock``: a validated system path
    (invitation, SSO) or an approved join request (#26). Reactivating a previous membership is
    a human decision; automatic admissions look for one first and create a request instead.
    """
    from skeleton.tenancy.models import Membership

    from .models import MemberRole

    if current_context().organization_id != organization.pk:
        raise PermissionDenied("Admission runs in the organization's own context.")
    _tenant_relation(role)
    if user.deleted_at is not None or not user.is_active:
        raise ValidationError(gettext("Identidade indisponível."))
    from skeleton.events.core_events import membership_changed

    membership = Membership.all_objects.filter(organization=organization, user=user).first()
    was_active = membership is not None and membership.is_active and membership.deleted_at is None
    if membership is None:
        membership = Membership(organization=organization, user=user, is_active=True)
        membership.save()
    elif membership.deleted_at is not None:
        membership.is_active = True
        membership.restore()
    elif not membership.is_active:
        membership.is_active = True
        membership.save(update_fields={"is_active"})
    if not MemberRole.objects.filter(membership=membership, role=role).exists():
        MemberRole(organization=organization, membership=membership, role=role).save()
    membership_changed(membership, was_active=was_active)
    return membership


def create_membership(*, user: User | None, data: Mapping[str, Any]) -> Membership:
    from skeleton.tenancy.models import Membership

    if set(data) - {"user", "is_active"}:
        raise ValidationError(gettext("Campos não permitidos."))
    with organization_lock(user, capability=MEMBERS_MANAGE) as organization:
        identity = data["user"]
        if identity.deleted_at is not None or not identity.is_active:
            raise ValidationError(gettext("Identidade indisponível."))
        if user is not None and identity.is_service_account:
            # Same rule as update/delete: only integrations.manage touches a service account's membership (#268).
            from .capabilities import INTEGRATIONS_MANAGE

            require_capability(user, INTEGRATIONS_MANAGE)
        from skeleton.events.core_events import membership_changed

        existing = Membership.all_objects.filter(organization=organization, user=identity).first()
        if existing:
            if existing.deleted_at is not None:
                existing.is_active = data.get("is_active", True)
                existing.restore()
                membership_changed(existing, was_active=False)
            else:
                return update_membership(existing, user=user, data={"is_active": data.get("is_active", True)})
            ensure_last_admin()
            return existing
        record = Membership(organization=organization, **data)
        record.save()
        membership_changed(record, was_active=False)
        return record


def revoke_membership_links(record: Membership) -> None:
    from .models import GroupMember, MemberRole, RecordGrant

    for model in (MemberRole, GroupMember, RecordGrant):
        model.objects.filter(membership=record).delete()


def update_membership(record: Membership, *, user: User | None, data: Mapping[str, Any]) -> Membership:
    if set(data) - {"is_active"}:
        raise ValidationError(gettext("A pessoa de um vínculo não muda."))
    with organization_lock(user, capability=MEMBERS_MANAGE):
        if record.organization_id != current_context().organization_id or record.deleted_at is not None:
            raise ValidationError(gettext("Vínculo indisponível."))
        require_service_account_manager(user, record)
        if data.get("is_active") is False:
            require_unprivileged_target(user, membership=record)
            revoke_membership_links(record)
        from skeleton.events.core_events import membership_changed

        was_active = record.is_active
        record.is_active = data.get("is_active", record.is_active)
        record.save(update_fields={"is_active"})
        membership_changed(record, was_active=was_active)
        ensure_last_admin()
        return record


def delete_membership(record: Membership, *, user: User | None) -> None:
    with organization_lock(user, capability=MEMBERS_MANAGE):
        if record.organization_id != current_context().organization_id or record.deleted_at is not None:
            raise ValidationError(gettext("Vínculo indisponível."))
        require_service_account_manager(user, record)
        require_unprivileged_target(user, membership=record)
        revoke_membership_links(record)
        from skeleton.events.core_events import membership_changed

        was_active = record.is_active
        record.is_active = False
        record.save(update_fields={"is_active"})
        record.delete()
        membership_changed(record, was_active=was_active)
        ensure_last_admin()


def system_deactivate_membership(record: Membership) -> Membership:
    """Deactivate like an administrator would, from a validated system context (directory sync, #33).

    Revokes ``MemberRole``/``GroupMember``/``RecordGrant`` of the membership, keeps it (inactive,
    with its history) and ends with ``ensure_last_admin()``, which raises ``LastAdministrator``
    and lets the caller's savepoint roll this one membership back. Never reactivates anyone.
    """
    context = current_context()
    if not context.system or record.organization_id != context.organization_id:
        raise PermissionDenied("Directory deactivation requires the organization's system context.")
    from skeleton.events.core_events import membership_changed

    with organization_lock():
        if record.deleted_at is not None or not record.is_active:
            return record
        revoke_membership_links(record)
        record.is_active = False
        record.save(update_fields={"is_active"})
        membership_changed(record, was_active=True)
        ensure_last_admin()
        return record


def leave_organization(user: User, *, expected_revision: int | None = None) -> Membership:
    """The actor leaves the current organization; anyone may, except its last administrator.

    Takes ``organization_lock()`` without a capability (leaving needs none), then locks the
    actor's own membership: the same Organization → membership order as every administrative
    change. Revokes ``MemberRole``/``GroupMember``/``RecordGrant`` of the membership, deactivates
    and soft-deletes it (same lifecycle as removal by an administrator, ids and history kept)
    and ends with ``ensure_last_admin()``, which rolls everything back with ``LastAdministrator``.
    ``expected_revision`` is the membership revision the caller showed (``ConcurrentUpdate``).
    Coming back requires an invitation, an approval or a designation, never an automatic flow.
    """
    from skeleton.common.exceptions import ConcurrentUpdate
    from skeleton.tenancy.models import Membership

    context = current_context()
    if not getattr(user, "is_authenticated", False) or context.actor != user.pk:
        raise PermissionDenied("Only the authenticated member can leave.")
    with organization_lock():
        membership = Membership.objects.select_for_update().filter(user=user, is_active=True).first()
        if membership is None:
            raise PermissionDenied(gettext("Vínculo indisponível."))
        if expected_revision is not None and membership.revision != expected_revision:
            raise ConcurrentUpdate("The membership changed.")
        revoke_membership_links(membership)
        from skeleton.events.core_events import membership_changed

        membership.is_active = False
        membership.save(update_fields={"is_active"})
        membership.delete()
        membership_changed(membership, was_active=True)
        ensure_last_admin()
        return membership


def create_role(*, user: User | None, data: Mapping[str, Any]) -> Role:
    from .models import Role

    if data.get("code") in BUILTIN_CODES:
        raise ValidationError(gettext("Esse código é reservado aos papéis internos."))
    return _create(Role, user=user, data=data, fields=("code", "name", "description"))


def update_role(record: Role, *, user: User | None, data: Mapping[str, Any]) -> Role:
    _require_type(record, _roles())
    if not record.is_builtin and data.get("code") in BUILTIN_CODES:
        raise ValidationError(gettext("Esse código é reservado aos papéis internos."))
    return _update(record, user=user, data=data, fields=("code", "name", "description"), allowed=_roles())


def delete_role(record: Role, *, user: User | None) -> None:
    return _delete(record, user=user, allowed=_roles())


def create_group(*, user: User | None, data: Mapping[str, Any]) -> AccessGroup:
    from .models import AccessGroup

    return _create(AccessGroup, user=user, data=data, fields=("code", "name", "description"))


def update_group(record: AccessGroup, *, user: User | None, data: Mapping[str, Any]) -> AccessGroup:
    return _update(record, user=user, data=data, fields=("code", "name", "description"), allowed=_groups())


def delete_group(record: AccessGroup, *, user: User | None) -> None:
    return _delete(record, user=user, allowed=_groups())


def assign_member_role(*, user: User | None, data: Mapping[str, Any]) -> MemberRole:
    from .models import MemberRole

    return _create(MemberRole, user=user, data=data, fields=("membership", "role"))


def add_group_member(*, user: User | None, data: Mapping[str, Any]) -> GroupMember:
    from .models import GroupMember

    return _create(GroupMember, user=user, data=data, fields=("group", "membership"))


def assign_group_role(*, user: User | None, data: Mapping[str, Any]) -> GroupRole:
    from .models import GroupRole

    return _create(GroupRole, user=user, data=data, fields=("group", "role"))


def _eligible_table(table: TableDefinition, action: str) -> TableSpec:
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import get_spec

    from .policies import ACTIONS

    try:
        spec = get_spec(table.model_label)
    except KeyError:
        raise ValidationError(gettext("Tabela de negócio desconhecida.")) from None
    if not table.available or not spec.expose_api or not issubclass(spec.model, SecuredRecord) or action not in ACTIONS:
        raise ValidationError(gettext("Essa tabela ou ação não aceita permissões."))
    return spec


def grant_table_permission(*, user: User | None, data: Mapping[str, Any]) -> TablePermission:
    from .models import TablePermission

    _eligible_table(data["table"], data["action"])
    return _create(TablePermission, user=user, data=data, fields=("role", "table", "action"))


def create_record_grant(*, user: User, data: Mapping[str, Any]) -> RecordGrant:
    from .models import RecordGrant

    spec = _eligible_table(data["table"], data["action"])
    if (
        data["action"] not in {"read", "update", "delete"}
        or sum(data.get(name) is not None for name in ("membership", "group", "role")) != 1
    ):
        raise ValidationError(gettext("A concessão precisa de exatamente um destinatário e de uma ação suportada."))
    # Resolve actual live record with authorized table/tenant visibility, never UUID-only trust.
    from .policies import authorized_queryset

    if not authorized_queryset(spec.model, user=user).filter(pk=data["record_sysid"]).exists():
        raise ValidationError(gettext("Registro indisponível."))
    return _create(
        RecordGrant, user=user, data=data, fields=("table", "record_sysid", "membership", "group", "role", "action")
    )


def revoke_link(record: Any, *, user: User | None) -> None:
    return _delete(record, user=user, allowed=_links())


def reconcile_builtin_roles(*, organization_id: UUID, using: str = "default") -> list[str]:
    """Create missing builtin roles and the administrator's table permissions.

    Idempotent and one organization per call (the caller loops in independent
    transactions). Returns the codes of builtin roles that could not be created
    because a custom role already uses the code; those must be renamed first.
    """
    from skeleton.system.models import TableDefinition
    from skeleton.system.registry import all_specs
    from skeleton.tenancy.context import system_context

    from .models import Role, TablePermission
    from .policies import ACTIONS

    collisions: list[str] = []
    with system_context(reason="builtin-roles", organization_id=organization_id, using=using):
        existing = {role.code: role for role in Role.objects.using(using).filter(code__in=BUILTIN_CODES)}
        for code, builtin in BUILTIN_ROLES.items():
            role = existing.get(code)
            if role is None:
                Role(organization_id=organization_id, code=code, name=str(builtin.name), is_builtin=True).save(
                    using=using
                )
            elif not role.is_builtin:
                collisions.append(code)
        role = Role.objects.using(using).filter(code="organization_admin", is_builtin=True).first()
        if role is None:
            return collisions
        # Two queries whatever the number of exposed tables (#284); the writes keep the order of
        # all_specs() and then of the sorted actions, so the audit comes out as before.
        exposed = [spec.model._meta.label_lower for spec in all_specs() if spec.expose_api]
        tables: dict[str, TableDefinition] = {}
        for table in (
            TableDefinition.objects.using(using).filter(model_label__in=exposed, available=True).order_by("sysid")
        ):
            tables.setdefault(table.model_label, table)  # The row .first() picked: the lowest primary key.
        granted = set(
            TablePermission.objects.using(using)
            .filter(role=role, table__in=list(tables.values()))
            .values_list("table_id", "action")
        )
        for label in exposed:
            table = tables.get(label)
            if table is None:
                continue
            for action in sorted(ACTIONS):
                if (table.pk, action) not in granted:
                    TablePermission(organization_id=organization_id, role=role, table=table, action=action).save(
                        using=using
                    )
    return collisions


def assert_user_deactivation(user: User, *, using: str = "default") -> None:
    """Narrow global identity guard, retaining organization locks until caller commit."""
    from django.db import IntegrityError, connections

    context = current_context()
    connection = connections[using]
    if context.organization_id is not None or not (context.platform_admin or context.system):
        raise PermissionDenied("Platform control plane required.")
    if not connection.in_atomic_block:
        raise RuntimeError("Identity lifecycle guard requires the caller's atomic transaction.")
    # The migration-owned SECURITY DEFINER function checks every organization (PostgreSQL only).
    try:
        with transaction.atomic(using=using), connection.cursor() as cursor:
            cursor.execute("SELECT skeleton.assert_user_deactivation(%s::uuid)", [str(user.pk)])
    except IntegrityError as exc:
        if getattr(exc.__cause__, "sqlstate", None) == "23514":
            raise LastAdministrator("An organization must retain an active administrator.") from exc
        raise
