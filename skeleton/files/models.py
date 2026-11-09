from django.db import models
from django.db.models import Q

from skeleton.common.models import OrganizationRecord


class Attachment(OrganizationRecord):
    """Metadata of a file attached to a record; authorization always derives from the target."""

    class State(models.TextChoices):
        PENDING = "pending", "Aguardando verificação"
        AVAILABLE = "available", "Disponível"
        QUARANTINED = "quarantined", "Em quarentena"
        REJECTED = "rejected", "Rejeitado"

    target_table = models.ForeignKey("sk_system.TableDefinition", on_delete=models.PROTECT, related_name="attachments")
    target_sysid = models.UUIDField()
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_key = models.CharField(max_length=200, unique=True, editable=False)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    scanned_at = models.DateTimeField(null=True, blank=True)
    scan_result_code = models.CharField(max_length=64, blank=True, default="")
    immutable_fields = ("target_table_id", "target_sysid", "filename", "content_type", "size", "sha256", "storage_key")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=Q(state__in=("pending", "available", "quarantined", "rejected")),
                name="sk_files_attachment_state",
            ),
        ]
        indexes = [
            models.Index(fields=("organization", "target_table", "target_sysid"), name="sk_files_attachment_target")
        ]

    def __str__(self):
        return self.filename
