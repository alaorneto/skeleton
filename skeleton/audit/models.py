from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from skeleton.common.models import SystemRecord


class AuditEvent(SystemRecord):
    class Operation(models.TextChoices):
        CREATE = "create", "Criar"
        UPDATE = "update", "Atualizar"
        SOFT_DELETE = "soft_delete", "Excluir logicamente"
        RESTORE = "restore", "Restaurar"
        HARD_DELETE = "hard_delete", "Excluir fisicamente"

    organization_id = models.UUIDField(null=True, blank=True, db_index=True)
    table_name = models.CharField(max_length=255)
    model_label = models.CharField(max_length=255)
    record_sysid = models.UUIDField(db_index=True)
    record_revision = models.PositiveBigIntegerField()
    operation = models.CharField(max_length=16, choices=Operation.choices)
    actor_sysid = models.UUIDField(null=True, blank=True)
    request_id = models.UUIDField(null=True, blank=True)
    source = models.CharField(max_length=255)
    db_principal = models.CharField(max_length=255)
    changes = models.JSONField(default=dict)
    append_only = True

    class Meta(SystemRecord.Meta):
        ordering = ("-created_at", "sysid")
        indexes = [models.Index(fields=("organization_id", "created_at"), name="sk_audit_org_created"), models.Index(fields=("table_name", "record_sysid", "record_revision"), name="sk_audit_record_revision")]
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(deleted_at__isnull=True, deleted_by__isnull=True, revision=1), name="sk_audit_event_append_only_metadata"),
            models.CheckConstraint(condition=Q(record_revision__gte=1), name="sk_audit_record_revision_positive"),
            models.CheckConstraint(condition=Q(operation__in=("create", "update", "soft_delete", "restore", "hard_delete")), name="sk_audit_event_operation"),
        ]

    def clean(self):
        # organization_id is historical UUID metadata, not a tenant FK.
        models.Model.clean(self)
        if self.deleted_at is not None or self.deleted_by is not None or self.revision != 1:
            raise ValidationError("Audit events are append-only.")
