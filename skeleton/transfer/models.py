"""State of CSV exports and imports (#182, #183).

``TransferJob`` is pipeline state, so it derives from ``OperationalRecord``: UUID, tenant
isolation and identity guard, no revision, tombstone or per-row audit. The file it produces or
reads lives in the private storage under ``org/<organization>/transfers/``; only the requester
downloads it, and ``transfer.retention`` deletes row and bytes after the retention window.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy

from skeleton.common.models import OperationalRecord


class TransferJob(OperationalRecord):
    class Kind(models.TextChoices):
        EXPORT = "export", gettext_lazy("Exportação")
        IMPORT = "import", gettext_lazy("Importação")

    class State(models.TextChoices):
        QUEUED = "queued", gettext_lazy("Na fila")
        RUNNING = "running", gettext_lazy("Em andamento")
        READY = "ready", gettext_lazy("Pronto")
        PREVIEWED = "previewed", gettext_lazy("Prévia pronta")
        APPLYING = "applying", gettext_lazy("Aplicando")
        APPLIED = "applied", gettext_lazy("Aplicado")
        FAILED = "failed", gettext_lazy("Falhou")

    organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="sk_transfer_jobs"
    )
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    table_key = models.CharField(max_length=100)
    filters = models.JSONField(default=dict)
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    row_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True, default="")
    file_key = models.CharField(max_length=300, blank=True, default="")
    report_key = models.CharField(max_length=300, blank=True, default="")
    filename = models.CharField(max_length=255, blank=True, default="")
    expires_at = models.DateTimeField()
    immutable_fields = ("requester_id", "kind", "table_key")

    class Meta(OperationalRecord.Meta):
        indexes = [
            models.Index(fields=("organization", "requester", "created_at"), name="sk_transfer_requester_idx"),
            models.Index(fields=("organization", "expires_at"), name="sk_transfer_expiry_idx"),
        ]

    def __str__(self):
        return f"{self.kind} {self.table_key} {self.state}"
