from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from skeleton.common.exceptions import ImmutableRecord, LiveReferencesExist
from skeleton.common.models import OrganizationRecord


class Action(models.TextChoices):
    READ = "read", "Ler"
    CREATE = "create", "Criar"
    UPDATE = "update", "Atualizar"
    DELETE = "delete", "Excluir"
    RESTORE = "restore", "Restaurar"
    MANAGE_ACCESS = "manage_access", "Gerir acesso"
    AUDIT = "audit", "Auditar"


def live_unique(fields, name):
    return models.UniqueConstraint(fields=fields, condition=Q(deleted_at__isnull=True), name=name, violation_error_code='unique')


class Role(OrganizationRecord):
    code = models.SlugField(max_length=64)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    is_builtin = models.BooleanField(default=False, editable=False)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [live_unique(("organization", "code"), "sk_access_role_live_code")]

    def clean(self):
        super().clean()
        if self.is_builtin and self.code not in {"organization_admin", "member"}:
            raise ValidationError({"code": "Unknown builtin role."})
        if self.is_builtin and self.deleted_at is not None:
            raise LiveReferencesExist("Builtin roles cannot be deleted.")
        if not self._state.adding:
            original = type(self).all_objects.filter(pk=self.pk).values("is_builtin", "code").first()
            if original and original["is_builtin"] and (not self.is_builtin or self.code != original["code"]):
                raise ImmutableRecord("Builtin roles cannot change their code or builtin status.")

    def delete(self, using=None, keep_parents=False):
        if self.is_builtin:
            raise LiveReferencesExist("Builtin roles cannot be deleted.")
        return super().delete(using=using, keep_parents=keep_parents)

    def __str__(self):
        return self.name


class MemberRole(OrganizationRecord):
    membership = models.ForeignKey("sk_tenancy.Membership", on_delete=models.PROTECT, related_name="member_roles")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="member_roles")
    immutable_fields = ("membership_id", "role_id")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [live_unique(("membership", "role"), "sk_access_memberrole_live_pair")]

    def clean(self):
        super().clean()
        validate_active_membership(self)


class AccessGroup(OrganizationRecord):
    code = models.SlugField(max_length=64)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [live_unique(("organization", "code"), "sk_access_accessgroup_live_code")]

    def __str__(self):
        return self.name


class GroupMember(OrganizationRecord):
    group = models.ForeignKey(AccessGroup, on_delete=models.PROTECT, related_name="group_members")
    membership = models.ForeignKey("sk_tenancy.Membership", on_delete=models.PROTECT, related_name="group_members")
    immutable_fields = ("group_id", "membership_id")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [live_unique(("group", "membership"), "sk_access_groupmember_live_pair")]

    def clean(self):
        super().clean()
        validate_active_membership(self)


class GroupRole(OrganizationRecord):
    group = models.ForeignKey(AccessGroup, on_delete=models.PROTECT, related_name="group_roles")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="group_roles")
    immutable_fields = ("group_id", "role_id")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [live_unique(("group", "role"), "sk_access_grouprole_live_pair")]


class TablePermission(OrganizationRecord):
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="table_permissions")
    table = models.ForeignKey("sk_system.TableDefinition", on_delete=models.PROTECT, related_name="table_permissions")
    action = models.CharField(max_length=16, choices=Action.choices)
    immutable_fields = ("role_id", "table_id", "action")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            live_unique(("role", "table", "action"), "sk_access_tablepermission_live_triple"),
            models.CheckConstraint(condition=Q(action__in=Action.values), name="sk_access_tablepermission_action"),
        ]


class RecordGrant(OrganizationRecord):
    table = models.ForeignKey("sk_system.TableDefinition", on_delete=models.PROTECT, related_name="record_grants")
    record_sysid = models.UUIDField()
    membership = models.ForeignKey("sk_tenancy.Membership", on_delete=models.PROTECT, null=True, blank=True, related_name="record_grants")
    group = models.ForeignKey(AccessGroup, on_delete=models.PROTECT, null=True, blank=True, related_name="record_grants")
    role = models.ForeignKey(Role, on_delete=models.PROTECT, null=True, blank=True, related_name="record_grants")
    action = models.CharField(max_length=16, choices=[(action.value, action.label) for action in (Action.READ, Action.UPDATE, Action.DELETE)])
    immutable_fields = ("table_id", "record_sysid", "membership_id", "group_id", "role_id", "action")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(condition=(Q(membership__isnull=False, group__isnull=True, role__isnull=True) | Q(membership__isnull=True, group__isnull=False, role__isnull=True) | Q(membership__isnull=True, group__isnull=True, role__isnull=False)), name="sk_access_recordgrant_one_subject"),
            models.CheckConstraint(condition=Q(action__in=("read", "update", "delete")), name="sk_access_recordgrant_action"),
            live_unique(("organization", "table", "record_sysid", "membership", "action"), "sk_access_recordgrant_live_member"),
            live_unique(("organization", "table", "record_sysid", "group", "action"), "sk_access_recordgrant_live_group"),
            live_unique(("organization", "table", "record_sysid", "role", "action"), "sk_access_recordgrant_live_role"),
        ]

    def clean(self):
        super().clean()
        validate_active_membership(self)


def validate_active_membership(record):
    if record.deleted_at is not None or not record.membership_id:
        return
    from skeleton.tenancy.models import Membership

    if not Membership.all_objects.filter(pk=record.membership_id, organization_id=record.organization_id, deleted_at__isnull=True, is_active=True, user__is_active=True, user__deleted_at__isnull=True, organization__is_active=True, organization__deleted_at__isnull=True).exists():
        raise ValidationError({"membership": "An active membership in this organization is required."})
