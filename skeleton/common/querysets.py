from django.db import models, transaction
from django.db.models import F
from django.utils import timezone

from .exceptions import MissingTenantContext, UnsafeBulkMutation


def context():
    from skeleton.tenancy.context import current_context

    return current_context()


def _remove_live_node(node, marker):
    node.children = [child for child in node.children if child is not marker]
    for child in node.children:
        if hasattr(child, "children"):
            _remove_live_node(child, marker)


class RecordQuerySet(models.QuerySet):
    _live_node = None

    def _clone(self):
        clone = super()._clone()
        clone._live_node = self._live_node
        return clone

    def with_deleted(self):
        clone = self._chain()
        if clone._live_node is not None:
            _remove_live_node(clone.query.where, clone._live_node)
            clone._live_node = None
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
                        resolved = self.filter(pk=record.pk).annotate(_sk_update_value=value).values_list("_sk_update_value", flat=True).get()
                    setattr(record, name, resolved)
                record.full_clean()
                for name, value in tuple(kwargs.items()):
                    if not hasattr(value, "resolve_expression"):
                        kwargs[name] = getattr(record, name)
            kwargs["updated_at"] = timezone.now()
            kwargs["updated_by"] = context().actor
            kwargs["revision"] = F("revision") + 1
            return models.QuerySet.update(self, **kwargs)

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False, update_fields=None, unique_fields=None):
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
        if any(field.name == "organization" for field in self.model._meta.fields):
            tenant = context().organization_id
            if tenant is None:
                raise MissingTenantContext("Organizational records require an explicit tenant context.")
            queryset = queryset.filter(organization_id=tenant)
        queryset = queryset.filter(deleted_at__isnull=True)
        queryset._live_node = queryset.query.where.children[-1]
        return queryset


class InfrastructureManager(models.Manager.from_queryset(RecordQuerySet)):
    """Unfiltered Django relation/infrastructure access; never an HTTP boundary."""
