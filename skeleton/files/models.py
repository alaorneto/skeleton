from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy

from skeleton.common.crypto import EncryptedTextField
from skeleton.common.models import OperationalRecord, OrganizationRecord, SystemRecord


class Attachment(OrganizationRecord):
    """Metadata of a file attached to a record; authorization always derives from the target."""

    class State(models.TextChoices):
        PENDING = "pending", gettext_lazy("Aguardando verificação")
        AVAILABLE = "available", gettext_lazy("Disponível")
        QUARANTINED = "quarantined", gettext_lazy("Em quarentena")
        REJECTED = "rejected", gettext_lazy("Rejeitado")
        # Bytes deleted after the retention of a deleted attachment (#192); never restorable.
        PURGED = "purged", gettext_lazy("Expurgado")

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
                condition=Q(state__in=("pending", "available", "quarantined", "rejected", "purged")),
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


STORAGE_KEYS = "FILES_STORAGE_ENCRYPTION_KEYS"


class StorageLocation(SystemRecord):
    """A place of the platform where organizations' files may live (#704): an S3 bucket or a test directory.

    The secret is encrypted with ``FILES_STORAGE_ENCRYPTION_KEYS`` and never shown. Only the
    platform administrator writes it (``skeleton.files.locations``); an organization never sees
    which location holds its files (#705).
    """

    class Backend(models.TextChoices):
        S3 = "s3", "S3"
        FILESYSTEM = "filesystem", gettext_lazy("Diretório local (desenvolvimento)")

    code = models.SlugField(max_length=64, unique=True)
    label = models.CharField(max_length=200)
    backend = models.CharField(max_length=16, choices=Backend.choices)
    bucket_name = models.CharField(max_length=63, blank=True)
    region_name = models.CharField(max_length=64, blank=True)
    endpoint_url = models.CharField(max_length=300, blank=True)
    addressing_style = models.CharField(max_length=8, default="auto")
    key_prefix = models.CharField(max_length=100, blank=True)
    access_key_id = models.CharField(max_length=128, blank=True)
    secret_access_key = EncryptedTextField(keyring=STORAGE_KEYS, blank=True, default="")
    directory = models.CharField(max_length=300, blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    immutable_fields = ("code", "backend")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=Q(backend__in=("s3", "filesystem")), name="sk_files_storagelocation_backend"
            ),
            models.CheckConstraint(
                condition=Q(addressing_style__in=("auto", "path", "virtual")),
                name="sk_files_storagelocation_addressing",
            ),
            models.UniqueConstraint(
                fields=("is_default",),
                condition=Q(is_default=True, deleted_at__isnull=True),
                name="sk_files_storagelocation_one_default",
                violation_error_code="unique",
            ),
        ]

    def __str__(self):
        return self.label


class OrganizationStorage(SystemRecord):
    """The storage location that holds one organization's files (#705); without it, the instance's."""

    storage_organization = models.OneToOneField(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="storage_assignment"
    )
    location = models.ForeignKey(StorageLocation, on_delete=models.PROTECT, related_name="organizations")
    immutable_fields = ("storage_organization_id",)

    def __str__(self):
        return f"{self.storage_organization_id} → {self.location_id}"
