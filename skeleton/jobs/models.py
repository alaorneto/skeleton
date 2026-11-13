from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy

from skeleton.common.models import OperationalRecord


class Job(OperationalRecord):
    """One unit of queued work; global so a worker claims jobs of many organizations without a GUC.

    The payload carries only identifiers and short scalars, never business data;
    handlers re-read what they need inside their own context.
    """

    class State(models.TextChoices):
        QUEUED = "queued", gettext_lazy("Na fila")
        RUNNING = "running", gettext_lazy("Executando")
        SUCCEEDED = "succeeded", gettext_lazy("Concluído")
        # Reserved: no code path sets it today (a failure goes back to "queued" or ends "dead").
        FAILED = "failed", gettext_lazy("Falhou")
        DEAD = "dead", gettext_lazy("Esgotado")
        CANCELLED = "cancelled", gettext_lazy("Cancelado")

    job_type = models.CharField(max_length=100)
    queue = models.CharField(max_length=64, default="default")
    # Not "organization": the table is global; the INSERT guard compares this column to the tenant GUC.
    job_organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    actor_id = models.UUIDField(null=True, blank=True)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    run_after = models.DateTimeField()
    locked_by = models.CharField(max_length=200, blank=True, default="")
    locked_until = models.DateTimeField(null=True, blank=True)
    last_error_class = models.CharField(max_length=200, blank=True, default="")
    last_error_code = models.CharField(max_length=100, blank=True, default="")
    idempotency_key = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001 - NULL means "no key"
    finished_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("job_type", "job_organization_id", "actor_id", "payload", "idempotency_key")

    class Meta(OperationalRecord.Meta):
        constraints = [
            # One job per key in each organization, and one per key among global jobs (#241):
            # a key chosen by organization A never collides with the same key in B.
            models.UniqueConstraint(
                fields=("job_type", "job_organization", "idempotency_key"),
                condition=Q(idempotency_key__isnull=False, job_organization__isnull=False),
                name="sk_jobs_job_idem_org",
                violation_error_code="unique",
            ),
            models.UniqueConstraint(
                fields=("job_type", "idempotency_key"),
                condition=Q(idempotency_key__isnull=False, job_organization__isnull=True),
                name="sk_jobs_job_idem_global",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=Q(state__in=("queued", "running", "succeeded", "failed", "dead", "cancelled")),
                name="sk_jobs_job_state",
            ),
        ]
        indexes = [
            models.Index(fields=("state", "queue", "run_after"), name="sk_jobs_job_claim"),
            # Operations of the queue (#308): the list by creation and the retention by finish time.
            models.Index(fields=("created_at",), name="sk_jobs_job_created"),
            models.Index(fields=("finished_at",), condition=Q(finished_at__isnull=False), name="sk_jobs_job_finished"),
        ]

    def __str__(self):
        return f"{self.job_type} {self.state}"


class WorkerHeartbeat(OperationalRecord):
    """Liveness of a worker process; leases of a silent worker expire and are requeued."""

    worker_id = models.CharField(max_length=200, unique=True)
    queues = models.CharField(max_length=500, blank=True, default="")
    state = models.CharField(max_length=16, default="running")
    last_seen = models.DateTimeField()
    immutable_fields = ("worker_id",)

    def __str__(self):
        return f"{self.worker_id} {self.state}"
