from django.db import models
from django.db.models import Q

from skeleton.common.models import OperationalRecord


class TransferJob(OperationalRecord):
    """One CSV export of a person, from the request to the file (docs/data-transfer.md).

    Pipeline state, not business data: no revision, tombstone or row audit. The row keeps
    the list filters as they were validated, the outcome and the private storage key;
    the job payload carries only this row's identifier. ``transfer.retention`` is the only
    path that deletes the row and its bytes.
    """

    class Kind(models.TextChoices):
        EXPORT = "export", "Exportação"

    class State(models.TextChoices):
        QUEUED = "queued", "Na fila"
        RUNNING = "running", "Gerando"
        SUCCEEDED = "succeeded", "Pronto"
        FAILED = "failed", "Falhou"

    organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="sk_transfer_transferjob_records"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.EXPORT)
    requested_by = models.UUIDField()
    table_key = models.CharField(max_length=128)
    filters = models.JSONField(default=dict)
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    row_count = models.PositiveIntegerField(null=True, blank=True)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    storage_key = models.CharField(max_length=200, blank=True, default="")
    filename = models.CharField(max_length=200, blank=True, default="")
    error_code = models.CharField(max_length=64, blank=True, default="")
    finished_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    idempotency_key = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001 - NULL means "no key"
    immutable_fields = ("kind", "requested_by", "table_key", "filters", "expires_at", "idempotency_key")

    class Meta(OperationalRecord.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "sysid"), name="sk_transfer_job_org_sysid", violation_error_code="unique"
            ),
            models.UniqueConstraint(
                fields=("organization", "requested_by", "idempotency_key"),
                condition=Q(idempotency_key__isnull=False),
                name="sk_transfer_job_idempotency",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=Q(state__in=("queued", "running", "succeeded", "failed")), name="sk_transfer_job_state"
            ),
        ]
        indexes = [
            models.Index(fields=("organization", "requested_by", "created_at"), name="sk_transfer_job_owner"),
            models.Index(fields=("organization", "expires_at"), name="sk_transfer_job_expiry"),
        ]

    def __str__(self):
        return f"{self.kind} {self.table_key} {self.state}"
