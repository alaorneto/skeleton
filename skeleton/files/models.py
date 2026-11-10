from django.db import models
from django.db.models import Q

from skeleton.common.models import OperationalRecord, OrganizationRecord, SystemRecord


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


class StorageUsage(OperationalRecord):
    """Bytes of live attachments of one organization, the counter the quota checks (#61).

    Writers lock the organization row first and this row second, inside the upload, delete
    or restore transaction; the recount job corrects any drift from the attachments.
    """

    organization = models.OneToOneField(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="sk_files_storage_usage"
    )
    bytes_used = models.PositiveBigIntegerField(default=0)
    recounted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.organization_id} {self.bytes_used}"


class StorageQuota(SystemRecord):
    """Platform override of the storage quota of one organization; without it the setting applies."""

    quota_organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="sk_files_storage_quotas"
    )
    # NULL means unlimited for this organization, whatever the default setting says.
    quota_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    immutable_fields = ("quota_organization_id",)

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("quota_organization",),
                condition=Q(deleted_at__isnull=True),
                name="sk_files_quota_organization_live",
                violation_error_code="unique",
            ),
        ]

    def __str__(self):
        return f"{self.quota_organization_id} {self.quota_bytes}"
