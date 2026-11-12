from django.db import models, transaction
from django.db.models import F
from django.db.models.lookups import IsNull
from django.utils import timezone

from .context import current_context, is_tenant_model
from .exceptions import MissingTenantContext, UnsafeBulkMutation


def context():
    return current_context()


class LiveRows(IsNull):
    """The manager's own ``deleted_at IS NULL``; ``with_deleted()`` removes it by type (#274)."""


def _without_live_rows(node):
    """A new WHERE tree without the manager's live-row filters (nodes may be shared: never mutate)."""
    clone = node.create(connector=node.connector, negated=node.negated)
    for child in node.children:
        if isinstance(child, LiveRows):
            continue
        clone.children.append(_without_live_rows(child) if hasattr(child, "children") else child)
    return clone


class RecordQuerySet(models.QuerySet):
    def with_deleted(self):
        """Remove only the tombstone filters of the manager, at any depth; never tenant or authorization."""
        clone = self._chain()
        clone.query.where = _without_live_rows(clone.query.where)
        return clone

    def update(self, **kwargs):
        forbidden = {"sysid", "pk", "organization", "organization_id", "created_at", "created_by", "revision"}
        forbidden.update(getattr(self.model, "immutable_fields", ()))
        forbidden.update(name[:-3] for name in tuple(forbidden) if name.endswith("_id"))
        if forbidden.intersection(kwargs):
            raise UnsafeBulkMutation("Immutable and system creation fields cannot be updated in bulk.")
        if getattr(self.model, "append_only", False):
            raise UnsafeBulkMutation("Append-only records cannot be updated.")
        if not kwargs:
            return 0
        with transaction.atomic(using=self.db):
            records = list(self.select_for_update())
            for record in records:
                for name, value in kwargs.items():
                    resolved = value
                    if hasattr(value, "resolve_expression"):
                        resolved = (
                            self.filter(pk=record.pk)
                            .annotate(_sk_update_value=value)
                            .values_list("_sk_update_value", flat=True)
                            .get()
                        )
                    setattr(record, name, resolved)
                record.full_clean()
                for name, value in tuple(kwargs.items()):
                    if not hasattr(value, "resolve_expression"):
                        kwargs[name] = getattr(record, name)
            kwargs["updated_at"] = timezone.now()
            kwargs["updated_by"] = context().actor
            kwargs["revision"] = F("revision") + 1
            return models.QuerySet.update(self, **kwargs)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if ignore_conflicts or update_conflicts:
            raise UnsafeBulkMutation("Conflict-skipping bulk inserts are not supported.")
        objs = list(objs)
        if getattr(self.model, "append_only", False):
            raise UnsafeBulkMutation("Audit records are written only by database triggers.")
        with transaction.atomic(using=self.db):
            for obj in objs:
                if not obj._state.adding:
                    raise UnsafeBulkMutation("bulk_create accepts only new records.")
                obj._state.db = self.db
                obj._prepare_create()
                obj.full_clean()
            created = super().bulk_create(objs, batch_size=batch_size)
            for obj in created:
                obj._loaded_values = {field.attname: getattr(obj, field.attname) for field in obj._meta.concrete_fields}
            return created

    def bulk_update(self, objs, fields, batch_size=None):
        raise UnsafeBulkMutation("bulk_update bypasses revision checks; use record services or update().")

    def delete(self):
        counts = {}
        with transaction.atomic(using=self.db):
            for obj in self.select_for_update():
                count, labels = obj.delete(using=self.db)
                for label, value in labels.items():
                    counts[label] = counts.get(label, 0) + value
        return sum(counts.values()), counts

    def restore(self):
        with transaction.atomic(using=self.db):
            count = 0
            for obj in self.select_for_update():
                if obj.deleted_at is not None:
                    obj.restore(using=self.db)
                    count += 1
            return count


class RecordManager(models.Manager.from_queryset(RecordQuerySet)):
    def get_queryset(self):
        queryset = super().get_queryset()
        if is_tenant_model(self.model):
            tenant = context().organization_id
            if tenant is None:
                raise MissingTenantContext("Organizational records require an explicit tenant context.")
            queryset = queryset.filter(organization_id=tenant)
        return queryset.filter(LiveRows(F("deleted_at"), True))


class InfrastructureManager(models.Manager.from_queryset(RecordQuerySet)):
    """Unfiltered Django relation/infrastructure access; never an HTTP boundary."""


class OperationalQuerySet(models.QuerySet):
    """Pipeline state: plain update/delete/bulk_create, never a revision-free bulk_update."""

    def bulk_update(self, objs, fields, batch_size=None):
        raise UnsafeBulkMutation("bulk_update is not supported on operational records.")

    def update(self, **kwargs):
        forbidden = {"sysid", "pk", "organization", "organization_id", "created_at"}
        forbidden.update(getattr(self.model, "immutable_fields", ()))
        forbidden.update(name[:-3] for name in tuple(forbidden) if name.endswith("_id"))
        if forbidden.intersection(kwargs):
            raise UnsafeBulkMutation("Immutable operational fields cannot be updated.")
        if not kwargs:
            return 0
        kwargs.setdefault("updated_at", timezone.now())
        return super().update(**kwargs)


class OperationalManager(models.Manager.from_queryset(OperationalQuerySet)):
    """Tenant-filtered when the model has an ``organization``; no tombstones exist."""

    def get_queryset(self):
        queryset = super().get_queryset()
        if is_tenant_model(self.model):
            tenant = context().organization_id
            if tenant is None:
                raise MissingTenantContext("Organizational records require an explicit tenant context.")
            queryset = queryset.filter(organization_id=tenant)
        return queryset


class OperationalInfrastructureManager(models.Manager.from_queryset(OperationalQuerySet)):
    """Unfiltered access for workers and retention services; never an HTTP boundary."""
