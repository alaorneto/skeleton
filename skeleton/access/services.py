"""All enterprise access changes are serialized on their organization row."""
from contextlib import contextmanager

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from skeleton.tenancy.context import current_context
from .policies import active_membership, is_organization_admin, effective_roles


class LastAdministrator(ValidationError):
    pass


def require_organization_admin(user):
    if not is_organization_admin(user):
        raise PermissionDenied("Organization administrator required.")


@contextmanager
def _locked(user=None):
    from skeleton.tenancy.models import Organization
    context = current_context()
    if context.organization_id is None:
        raise PermissionDenied("Organization required.")
    with transaction.atomic(using=context.using):
        organization = Organization.all_objects.using(context.using).select_for_update().get(pk=context.organization_id)
        if not organization.is_active or organization.deleted_at is not None:
            raise PermissionDenied("Organization unavailable.")
        if user is not None:
            require_organization_admin(user)
        yield organization


def ensure_last_admin():
    from skeleton.tenancy.models import Membership
    from .models import MemberRole, GroupMember, GroupRole
    direct = MemberRole.objects.filter(role__code="organization_admin", role__is_builtin=True,
                                      role__deleted_at__isnull=True).values_list("membership_id", flat=True)
    groups = GroupRole.objects.filter(role__code="organization_admin", role__is_builtin=True,
                                     role__deleted_at__isnull=True, group__deleted_at__isnull=True).values_list("group_id", flat=True)
    indirect = GroupMember.objects.filter(group_id__in=groups).values_list("membership_id", flat=True)
    from django.db.models import Q
    if not Membership.objects.filter(Q(pk__in=direct) | Q(pk__in=indirect), is_active=True,
                                       user__is_active=True, user__deleted_at__isnull=True).exists():
        raise LastAdministrator("An organization must retain an active administrator.")


def _tenant_relation(obj):
    if getattr(obj, "organization_id", current_context().organization_id) != current_context().organization_id or obj.deleted_at is not None:
        raise ValidationError("Relation must be live and belong to the current organization.")
    if hasattr(obj, "is_active") and not obj.is_active:
        raise ValidationError("Relation must be active.")


def _create(model, *, user, data, fields):
    if set(data) - set(fields):
        raise ValidationError("Unsupported fields.")
    with _locked(user) as organization:
        for value in data.values():
            if hasattr(value, "_meta"):
                _tenant_relation(value)
        record = model(organization=organization, **data)
        record.save()
        return record


def _update(record, *, user, data, fields):
    if set(data) - set(fields):
        raise ValidationError("Unsupported fields.")
    with _locked(user):
        _tenant_relation(record)
        if getattr(record, "is_builtin", False) and "code" in data and data["code"] != record.code:
            raise ValidationError("Builtin role codes are immutable.")
        for key, value in data.items():
            setattr(record, key, value)
        record.save(update_fields=set(data))
        ensure_last_admin()
        return record


def _delete(record, *, user):
    with _locked(user):
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
    with _locked():
        membership = Membership.all_objects.filter(organization=organization, user=user).first()
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
        return membership


def create_membership(*, user, data):
    from skeleton.tenancy.models import Membership
    if set(data) - {"user", "is_active"}:
        raise ValidationError("Unsupported fields.")
    with _locked(user) as organization:
        identity = data["user"]
        if identity.deleted_at is not None or not identity.is_active:
            raise ValidationError("Identity unavailable.")
        existing = Membership.all_objects.filter(organization=organization, user=identity).first()
        if existing:
            if existing.deleted_at is not None:
                existing.is_active = data.get("is_active", True)
                existing.restore()
            else:
                return update_membership(existing, user=user, data={"is_active": data.get("is_active", True)})
            ensure_last_admin()
            return existing
        record = Membership(organization=organization, **data)
        record.save()
        return record


def _revoke_membership_links(record):
    from .models import MemberRole, GroupMember, RecordGrant
    for model in (MemberRole, GroupMember, RecordGrant):
        model.objects.filter(membership=record).delete()


def update_membership(record, *, user, data):
    if set(data) - {"is_active"}:
        raise ValidationError("Membership identity is immutable.")
    with _locked(user):
        if record.organization_id != current_context().organization_id or record.deleted_at is not None:
            raise ValidationError("Membership unavailable.")
        if data.get("is_active") is False:
            _revoke_membership_links(record)
        record.is_active = data.get("is_active", record.is_active)
        record.save(update_fields={"is_active"})
        ensure_last_admin()
        return record


def delete_membership(record, *, user):
    with _locked(user):
        if record.organization_id != current_context().organization_id or record.deleted_at is not None:
            raise ValidationError("Membership unavailable.")
        _revoke_membership_links(record)
        record.is_active = False
        record.save(update_fields={"is_active"})
        record.delete()
        ensure_last_admin()


def create_role(*, user, data):
    from .models import Role
    if data.get("code") in {"organization_admin", "member"}:
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
        raise ValidationError("Unknown business table.")
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
    if data["action"] not in {"read", "update", "delete"} or sum(data.get(name) is not None for name in ("membership", "group", "role")) != 1:
        raise ValidationError("A record grant requires exactly one subject and a supported action.")
    # Resolve actual live record with authorized table/tenant visibility, never UUID-only trust.
    from .policies import authorized_queryset
    if not authorized_queryset(spec.model, user=user).filter(pk=data["record_sysid"]).exists():
        raise ValidationError("Record is unavailable.")
    return _create(RecordGrant, user=user, data=data, fields=("table", "record_sysid", "membership", "group", "role", "action"))


def revoke_link(record, *, user):
    return _delete(record, user=user)


def reconcile_builtin_permissions(*, organization_id, using="default"):
    from skeleton.tenancy.context import system_context
    from .models import Role, TablePermission
    from skeleton.system.models import TableDefinition
    from skeleton.system.registry import all_specs
    from .policies import ACTIONS
    with system_context(reason="builtin-permissions", organization_id=organization_id, using=using):
        role = Role.objects.filter(code="organization_admin", is_builtin=True).first()
        if role is None:
            return
        for spec in all_specs():
            if not spec.expose_api:
                continue
            table = TableDefinition.objects.using(using).filter(model_label=spec.model._meta.label_lower, available=True).first()
            if table:
                for action in sorted(ACTIONS):
                    if not TablePermission.objects.using(using).filter(role=role, table=table, action=action).exists():
                        TablePermission(organization_id=organization_id, role=role, table=table, action=action).save(using=using)


def assert_user_deactivation(user, *, using="default"):
    """Narrow global identity guard, retaining organization locks until caller commit."""
    from django.db import IntegrityError, connections
    from django.db.models import Q
    from skeleton.tenancy.models import Membership, Organization
    from .models import MemberRole, GroupMember, GroupRole
    context = current_context()
    connection = connections[using]
    if context.organization_id is not None or not (context.platform_admin or context.system):
        raise PermissionDenied("Platform control plane required.")
    if not connection.in_atomic_block:
        raise RuntimeError("Identity lifecycle guard requires the caller's atomic transaction.")
    if connection.vendor == "postgresql":
        try:
            with transaction.atomic(using=using), connection.cursor() as cursor:
                cursor.execute("SELECT skeleton.assert_user_deactivation(%s::uuid)", [str(user.pk)])
        except IntegrityError as exc:
            if getattr(exc.__cause__, "sqlstate", None) == "23514":
                raise LastAdministrator("An organization must retain an active administrator.") from exc
            raise
        return
    if connection.vendor != "sqlite":
        raise NotImplementedError("Identity lifecycle requires SQLite or PostgreSQL.")
    ids = Membership.all_objects.using(using).filter(user=user, is_active=True, deleted_at__isnull=True).values_list("organization_id", flat=True)
    organizations = Organization.all_objects.using(using).filter(pk__in=ids, is_active=True, deleted_at__isnull=True).order_by("sysid").select_for_update()
    for organization in organizations:
        org_id = organization.pk
        direct = MemberRole.all_objects.using(using).filter(organization_id=org_id, deleted_at__isnull=True,
            role__organization_id=org_id, role__code="organization_admin", role__is_builtin=True,
            role__deleted_at__isnull=True).values_list("membership_id", flat=True)
        groups = GroupRole.all_objects.using(using).filter(organization_id=org_id, deleted_at__isnull=True,
            role__organization_id=org_id, role__code="organization_admin", role__is_builtin=True,
            role__deleted_at__isnull=True, group__organization_id=org_id,
            group__deleted_at__isnull=True).values_list("group_id", flat=True)
        indirect = GroupMember.all_objects.using(using).filter(organization_id=org_id,
            deleted_at__isnull=True, group_id__in=groups).values_list("membership_id", flat=True)
        if not Membership.all_objects.using(using).filter(Q(pk__in=direct) | Q(pk__in=indirect),
                organization_id=org_id, is_active=True, deleted_at__isnull=True,
                user__is_active=True, user__deleted_at__isnull=True).exclude(user=user).exists():
            raise LastAdministrator("An organization must retain an active administrator.")
