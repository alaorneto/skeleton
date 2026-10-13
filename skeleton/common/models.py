import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, router, transaction
from django.db.models import Q
from django.utils import timezone

from .exceptions import ConcurrentUpdate, ImmutableRecord, LiveReferencesExist, MissingTenantContext, UnsafeBulkMutation
from .querysets import InfrastructureManager, RecordManager, context


class SystemRecord(models.Model):
    sysid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(default=timezone.now, editable=False)
    created_by = models.UUIDField(null=True, blank=True, editable=False)
    updated_by = models.UUIDField(null=True, blank=True, editable=False)
    deleted_by = models.UUIDField(null=True, blank=True, editable=False)
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)
    revision = models.PositiveBigIntegerField(default=1, editable=False)

    objects = RecordManager()
    all_objects = InfrastructureManager()
    immutable_fields = ()
    soft_delete_reference_policy = "protect"
    append_only = False

    class Meta:
        abstract = True
        base_manager_name = "all_objects"
        default_manager_name = "objects"
        constraints = [
            models.CheckConstraint(condition=Q(revision__gte=1), name="%(app_label)s_%(class)s_revision_positive"),
            models.CheckConstraint(condition=Q(deleted_by__isnull=True) | Q(deleted_at__isnull=False), name="%(app_label)s_%(class)s_deleted_actor"),
        ]

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_values = dict(zip(field_names, values))
        return instance

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        loaded = getattr(self, "_loaded_values", {}).copy()
        for field in self._meta.concrete_fields:
            if field.attname in self.__dict__ and (fields is None or field.name in fields or field.attname in fields):
                loaded[field.attname] = self.__dict__[field.attname]
        self._loaded_values = loaded

    def _ensure_snapshot(self, using):
        tracked = {"sysid", "revision", "created_at", "created_by", *self.immutable_fields}
        if hasattr(self, "organization_id"):
            tracked.add("organization_id")
        loaded = getattr(self, "_loaded_values", {})
        if not tracked.issubset(loaded):
            persisted = type(self).all_objects.using(using).filter(pk=loaded.get("sysid", self.pk)).values(*tracked).first()
            if persisted is None:
                raise ConcurrentUpdate("The record no longer exists.")
            loaded = {**persisted, **loaded}
            self._loaded_values = loaded
        return loaded

    def _prepare_create(self):
        now = timezone.now()
        self.created_at = now
        self.updated_at = now
        self.created_by = context().actor
        self.updated_by = context().actor
        self.revision = 1
        self._validate_context()

    def _validate_context(self):
        if hasattr(self, "organization_id"):
            tenant = context().organization_id
            if tenant is None:
                raise MissingTenantContext("An organizational write requires tenant context.")
            if self.organization_id != tenant:
                raise ValidationError({"organization": "Organization does not match the current tenant."})

    def clean(self):
        super().clean()
        self._validate_context()
        if self.deleted_at is not None and not self._state.adding:
            using = self._state.db or "default"
            if type(self).all_objects.using(using).filter(pk=self.pk, deleted_at__isnull=True).exists():
                self._check_live_references(using)
        if self.deleted_at is None:
            for field in self._meta.concrete_fields:
                if not field.is_relation or not getattr(self, field.attname, None):
                    continue
                target = field.remote_field.model
                if not issubclass(target, SystemRecord):
                    continue
                related = target.all_objects.using(self._state.db or "default").filter(pk=getattr(self, field.attname)).first()
                if related is None:
                    continue  # The FK field validator supplies the missing-target error.
                if related.deleted_at is not None and target.soft_delete_reference_policy != "retain":
                    raise ValidationError({field.name: "The referenced record is deleted."})
                if hasattr(self, "organization_id") and hasattr(related, "organization_id") and self.organization_id != related.organization_id:
                    raise ValidationError({field.name: "The referenced record belongs to another organization."})

    def save(self, *args, force_insert=False, force_update=False, using=None, update_fields=None):
        if args:
            raise TypeError("Use keyword arguments for record save().")
        if self.append_only:
            raise UnsafeBulkMutation("Audit records are written only by database triggers.")
        using = using or router.db_for_write(type(self), instance=self)
        self._state.db = using
        if update_fields is not None:
            update_fields = set(update_fields)
            if not update_fields:
                return
            valid_fields = {name for field in self._meta.concrete_fields if not field.primary_key for name in (field.name, field.attname)}
            if update_fields - valid_fields:
                raise ValueError("update_fields must contain existing non-primary-key concrete fields.")
        with transaction.atomic(using=using):
            if self._state.adding:
                if force_update:
                    raise ValueError("A new record cannot be force-updated.")
                self._prepare_create()
                self.full_clean()
                super().save(force_insert=True, using=using)
            else:
                if force_insert:
                    raise ValueError("An existing record cannot be force-inserted.")
                original = self._ensure_snapshot(using)
                for field in ("sysid", *self.immutable_fields, *(("organization_id",) if hasattr(self, "organization_id") else ())):
                    if getattr(self, field) != original[field]:
                        raise ImmutableRecord(f"{field} cannot be changed.")
                expected_revision = original["revision"]
                if self.revision != expected_revision:
                    raise ConcurrentUpdate("The supplied revision does not match the loaded record.")
                self.created_at = original["created_at"]
                self.created_by = original["created_by"]
                self.updated_at = timezone.now()
                self.updated_by = context().actor
                self.revision = expected_revision + 1
                self.full_clean()
                fields = [field for field in self._meta.concrete_fields if not field.primary_key]
                if update_fields is not None:
                    allowed = {"updated_at", "updated_by", "revision"} | update_fields
                    fields = [field for field in fields if field.name in allowed or field.attname in allowed]
                values = {field.attname: getattr(self, field.attname) for field in fields}
                queryset = type(self).all_objects.using(using).filter(pk=self.pk, revision=expected_revision)
                if models.QuerySet.update(queryset, **values) != 1:
                    self.revision = expected_revision
                    raise ConcurrentUpdate("Another writer changed this record.")
            self._loaded_values = {field.attname: getattr(self, field.attname) for field in self._meta.concrete_fields}

    def _check_live_references(self, using):
        if self.soft_delete_reference_policy == "retain":
            return
        for relation in self._meta.related_objects:
            target = relation.related_model
            if issubclass(target, SystemRecord):
                if target.all_objects.using(using).filter(**{relation.field.attname: self.pk}, deleted_at__isnull=True).exists():
                    raise LiveReferencesExist(f"Live {target._meta.label} records still reference this record.")

    def _prepare_soft_delete(self):
        return set()

    def _prepare_restore(self):
        return set()

    def delete(self, using=None, keep_parents=False):
        if self.append_only:
            raise UnsafeBulkMutation("Append-only records cannot be deleted.")
        if self._state.adding:
            raise ValueError("An unsaved record cannot be deleted.")
        using = using or self._state.db or "default"
        if self.deleted_at is not None:
            return 0, {}
        with transaction.atomic(using=using):
            self._check_live_references(using)
            self.deleted_at = timezone.now()
            self.deleted_by = context().actor
            fields = self._prepare_soft_delete()
            self.save(using=using, update_fields={"deleted_at", "deleted_by"} | fields)
        return 1, {self._meta.label: 1}

    def restore(self, using=None):
        if self.append_only:
            raise UnsafeBulkMutation("Append-only records cannot be restored.")
        if self._state.adding:
            raise ValueError("An unsaved record cannot be restored.")
        if self.deleted_at is None:
            return self
        using = using or self._state.db or "default"
        with transaction.atomic(using=using):
            self.deleted_at = None
            self.deleted_by = None
            fields = self._prepare_restore()
            self.save(using=using, update_fields={"deleted_at", "deleted_by"} | fields)
        return self


class OrganizationRecord(SystemRecord):
    organization = models.ForeignKey("sk_tenancy.Organization", on_delete=models.PROTECT, related_name="%(app_label)s_%(class)s_records")

    class Meta(SystemRecord.Meta):
        abstract = True
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(fields=("organization", "sysid"), name="%(app_label)s_%(class)s_org_sysid", violation_error_code='unique'),
        ]


class SecuredRecord(OrganizationRecord):
    class Visibility(models.TextChoices):
        PRIVATE = "private", "Privado"
        GROUP = "group", "Grupo"
        ORGANIZATION = "organization", "Organização"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="%(app_label)s_%(class)s_owned")
    visibility = models.CharField(max_length=16, choices=Visibility.choices, default=Visibility.PRIVATE)
    owning_group = models.ForeignKey("sk_access.AccessGroup", on_delete=models.PROTECT, null=True, blank=True, related_name="%(app_label)s_%(class)s_owned")

    class Meta(OrganizationRecord.Meta):
        abstract = True
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(condition=(Q(visibility="group", owning_group__isnull=False) | Q(visibility__in=("private", "organization"), owning_group__isnull=True)), name="%(app_label)s_%(class)s_visibility_group"),
        ]

    def clean(self):
        super().clean()
        if self.deleted_at is None and self.owner_id and self.organization_id:
            from skeleton.tenancy.models import Membership

            if not Membership.all_objects.filter(organization_id=self.organization_id, user_id=self.owner_id, is_active=True, deleted_at__isnull=True, user__is_active=True, user__deleted_at__isnull=True, organization__is_active=True, organization__deleted_at__isnull=True).exists():
                raise ValidationError({"owner": "The owner must have an active organization membership."})
