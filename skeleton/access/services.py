"""All enterprise access changes are serialized on their organization row."""

from contextlib import contextmanager

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

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


class LastAdministrator(ValidationError):
    pass


def require_organization_admin(user):
    from .policies import credential_scopes

    if credential_scopes() is not None or not is_organization_admin(user):
        raise PermissionDenied("Organization administrator required.")


@contextmanager
def organization_lock(user=None, *, capability=None):
    """Lock the organization before its records; HTTP callers must pass the actor.

    Without ``capability`` the actor must be the organization administrator;
    with it, the actor needs that capability of a builtin role.
    """
    from skeleton.tenancy.models import Organization

    context = current_context()
    if context.organization_id is None:
        raise PermissionDenied("Organization required.")
    with transaction.atomic(using=context.using):
        organization = Organization.all_objects.using(context.using).select_for_update().get(pk=context.organization_id)
        if not organization.is_active or organization.deleted_at is not None:
            raise PermissionDenied("Organization unavailable.")
        if user is not None:
            if capability is None:
                require_organization_admin(user)
            else:
                require_capability(user, capability)
        yield organization


def require_unprivileged_target(user, *, role=None, membership=None):
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


def administrator_memberships():
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


def require_service_account_manager(user, membership):
    """Only ``integrations.manage`` changes a service account's membership; a people manager cannot (#44)."""
    from .capabilities import INTEGRATIONS_MANAGE

    if user is not None and membership.user.is_service_account:
        require_capability(user, INTEGRATIONS_MANAGE)


def ensure_last_admin():
    if not administrator_memberships().exists():
        raise LastAdministrator("An organization must retain an active administrator.")


def _tenant_relation(obj):
    if (
        getattr(obj, "organization_id", current_context().organization_id) != current_context().organization_id
        or obj.deleted_at is not None
    ):
        raise ValidationError("Relation must be live and belong to the current organization.")
    if hasattr(obj, "is_active") and not obj.is_active:
        raise ValidationError("Relation must be active.")


def _create(model, *, user, data, fields, capability=ACCESS_MANAGE):
    if set(data) - set(fields):
        raise ValidationError("Unsupported fields.")
    with organization_lock(user, capability=capability) as organization:
        for value in data.values():
            if hasattr(value, "_meta"):
                _tenant_relation(value)
        record = model(organization=organization, **data)
        record.save()
        return record


def _update(record, *, user, data, fields, capability=ACCESS_MANAGE):
    if set(data) - set(fields):
        raise ValidationError("Unsupported fields.")
    with organization_lock(user, capability=capability):
        _tenant_relation(record)
        if getattr(record, "is_builtin", False) and "code" in data and data["code"] != record.code:
            raise ValidationError("Builtin role codes are immutable.")
        for key, value in data.items():
            setattr(record, key, value)
        record.save(update_fields=set(data))
        ensure_last_admin()
        return record


def _delete(record, *, user, capability=ACCESS_MANAGE):
    with organization_lock(user, capability=capability):
        _tenant_relation(record)
        if getattr(record, "is_builtin", False):
            raise ValidationError("Builtin roles cannot be removed.")
        record.delete()
        ensure_last_admin()


def provision_membership(*, organization, user, role):
    from skeleton.tenancy.models import Membership

    from .models import MemberRole

    context = current_context()
    if not context.system or context.organization_id != organization.pk:
        raise PermissionDenied("Provisioning requires a validated system tenant context.")
    _tenant_relation(role)
    if user.deleted_at is not None or not user.is_active:
        raise ValidationError("Identity is unavailable.")
    from skeleton.events.core_events import membership_changed

    with organization_lock():
        membership = Membership.all_objects.filter(organization=organization, user=user).first()
        was_active = membership is not None and membership.is_active and membership.deleted_at is None
        if membership is None:
            membership = Membership(organization=organization, user=user, is_active=True)
            membership.save()
        else:
            if membership.deleted_at is not None:
                membership.is_active = True
                membership.restore()
            elif not membership.is_active:
                membership.is_active = True
                membership.save(update_fields={"is_active"})
        if not MemberRole.objects.filter(membership=membership, role=role).exists():
            MemberRole(organization=organization, membership=membership, role=role).save()
        membership_changed(membership, was_active=was_active)
        return membership


def create_membership(*, user, data):
    from skeleton.tenancy.models import Membership

    if set(data) - {"user", "is_active"}:
        raise ValidationError("Unsupported fields.")
    with organization_lock(user, capability=MEMBERS_MANAGE) as organization:
        identity = data["user"]
        if identity.deleted_at is not None or not identity.is_active:
            raise ValidationError("Identity unavailable.")
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


def _revoke_membership_links(record):
    from .models import GroupMember, MemberRole, RecordGrant

    for model in (MemberRole, GroupMember, RecordGrant):
        model.objects.filter(membership=record).delete()


def update_membership(record, *, user, data):
    if set(data) - {"is_active"}:
        raise ValidationError("Membership identity is immutable.")
    with organization_lock(user, capability=MEMBERS_MANAGE):
        if record.organization_id != current_context().organization_id or record.deleted_at is not None:
            raise ValidationError("Membership unavailable.")
        require_service_account_manager(user, record)
        if data.get("is_active") is False:
            require_unprivileged_target(user, membership=record)
            _revoke_membership_links(record)
        from skeleton.events.core_events import membership_changed

        was_active = record.is_active
        record.is_active = data.get("is_active", record.is_active)
        record.save(update_fields={"is_active"})
        membership_changed(record, was_active=was_active)
        ensure_last_admin()
        return record


def delete_membership(record, *, user):
    with organization_lock(user, capability=MEMBERS_MANAGE):
        if record.organization_id != current_context().organization_id or record.deleted_at is not None:
            raise ValidationError("Membership unavailable.")
        require_service_account_manager(user, record)
        require_unprivileged_target(user, membership=record)
        _revoke_membership_links(record)
        from skeleton.events.core_events import membership_changed

        was_active = record.is_active
        record.is_active = False
        record.save(update_fields={"is_active"})
        record.delete()
        membership_changed(record, was_active=was_active)
        ensure_last_admin()


def leave_organization(user, *, expected_revision=None):
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
            raise PermissionDenied("Membership unavailable.")
        if expected_revision is not None and membership.revision != expected_revision:
            raise ConcurrentUpdate("The membership changed.")
        _revoke_membership_links(membership)
        from skeleton.events.core_events import membership_changed

        membership.is_active = False
        membership.save(update_fields={"is_active"})
        membership.delete()
        membership_changed(membership, was_active=True)
        ensure_last_admin()
        return membership


def create_role(*, user, data):
    from .models import Role

    if data.get("code") in BUILTIN_CODES:
        raise ValidationError("Builtin codes are reserved.")
    return _create(Role, user=user, data=data, fields=("code", "name", "description"))


def update_role(record, *, user, data):
    return _update(record, user=user, data=data, fields=("code", "name", "description"))


def delete_role(record, *, user):
    return _delete(record, user=user)


def create_group(*, user, data):
    from .models import AccessGroup

    return _create(AccessGroup, user=user, data=data, fields=("code", "name", "description"))


def update_group(record, *, user, data):
    return _update(record, user=user, data=data, fields=("code", "name", "description"))


def delete_group(record, *, user):
    return _delete(record, user=user)


def assign_member_role(*, user, data):
    from .models import MemberRole

    return _create(MemberRole, user=user, data=data, fields=("membership", "role"))


def add_group_member(*, user, data):
    from .models import GroupMember

    return _create(GroupMember, user=user, data=data, fields=("group", "membership"))


def assign_group_role(*, user, data):
    from .models import GroupRole

    return _create(GroupRole, user=user, data=data, fields=("group", "role"))


def _eligible_table(table, action):
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import get_spec

    from .policies import ACTIONS

    try:
        spec = get_spec(table.model_label)
    except KeyError:
        raise ValidationError("Unknown business table.") from None
    if not table.available or not spec.expose_api or not issubclass(spec.model, SecuredRecord) or action not in ACTIONS:
        raise ValidationError("Table or action is not eligible for enterprise permissions.")
    return spec


def grant_table_permission(*, user, data):
    from .models import TablePermission

    _eligible_table(data["table"], data["action"])
    return _create(TablePermission, user=user, data=data, fields=("role", "table", "action"))


def create_record_grant(*, user, data):
    from .models import RecordGrant

    spec = _eligible_table(data["table"], data["action"])
    if (
        data["action"] not in {"read", "update", "delete"}
        or sum(data.get(name) is not None for name in ("membership", "group", "role")) != 1
    ):
        raise ValidationError("A record grant requires exactly one subject and a supported action.")
    # Resolve actual live record with authorized table/tenant visibility, never UUID-only trust.
    from .policies import authorized_queryset

    if not authorized_queryset(spec.model, user=user).filter(pk=data["record_sysid"]).exists():
        raise ValidationError("Record is unavailable.")
    return _create(
        RecordGrant, user=user, data=data, fields=("table", "record_sysid", "membership", "group", "role", "action")
    )


def revoke_link(record, *, user):
    return _delete(record, user=user)


def reconcile_builtin_roles(*, organization_id, using="default"):
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

    collisions = []
    with system_context(reason="builtin-roles", organization_id=organization_id, using=using):
        existing = {role.code: role for role in Role.objects.using(using).filter(code__in=BUILTIN_CODES)}
        for code, builtin in BUILTIN_ROLES.items():
            role = existing.get(code)
            if role is None:
                Role(organization_id=organization_id, code=code, name=builtin.name, is_builtin=True).save(using=using)
            elif not role.is_builtin:
                collisions.append(code)
        role = Role.objects.using(using).filter(code="organization_admin", is_builtin=True).first()
        if role is None:
            return collisions
        for spec in all_specs():
            if not spec.expose_api:
                continue
            table = (
                TableDefinition.objects.using(using)
                .filter(model_label=spec.model._meta.label_lower, available=True)
                .first()
            )
            if table:
                for action in sorted(ACTIONS):
                    if not TablePermission.objects.using(using).filter(role=role, table=table, action=action).exists():
                        TablePermission(organization_id=organization_id, role=role, table=table, action=action).save(
                            using=using
                        )
    return collisions


def assert_user_deactivation(user, *, using="default"):
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
