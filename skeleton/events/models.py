from django.db import models
from django.utils import timezone

from skeleton.common.models import OperationalRecord


class DomainEvent(OperationalRecord):
    """Outbox row of one domain event; tenant RLS, correlated with AuditEvent by revision and request."""

    organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="sk_events_domainevent_records"
    )
    event_type = models.CharField(max_length=100)
    version = models.PositiveIntegerField(default=1)
    table_key = models.CharField(max_length=128)
    record_sysid = models.UUIDField()
    record_revision = models.PositiveBigIntegerField(null=True, blank=True)
    actor_id = models.UUIDField(null=True, blank=True)
    request_id = models.UUIDField(null=True, blank=True)
    source = models.CharField(max_length=200)
    data = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(default=timezone.now)
    immutable_fields = (
        "event_type",
        "version",
        "table_key",
        "record_sysid",
        "record_revision",
        "actor_id",
        "request_id",
        "source",
        "data",
        "occurred_at",
    )

    class Meta(OperationalRecord.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "sysid"), name="sk_events_domainevent_org_sysid", violation_error_code="unique"
            ),
        ]
        indexes = [models.Index(fields=("organization", "occurred_at"), name="sk_events_event_occurred")]

    def __str__(self):
        return f"{self.event_type} {self.record_sysid}"
