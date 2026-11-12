from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy

from skeleton.common.crypto import EncryptedTextField
from skeleton.common.models import OperationalRecord, OrganizationRecord

INTEGRATION_KEYS = "INTEGRATION_ENCRYPTION_KEYS"


class WebhookEndpoint(OrganizationRecord):
    """An organization's subscription of an HTTPS URL to event types, signed with a shared secret.

    ``principal`` is a service account of the same organization: deliveries act as it, so a
    receiver only learns about records the principal can read. Secrets are encrypted with the
    ``INTEGRATION_ENCRYPTION_KEYS`` keyring, redacted from audit and shown only once.
    """

    name = models.CharField(max_length=150)
    url = models.URLField(max_length=2000)
    principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    event_types = models.JSONField(default=list)
    secret_ciphertext = EncryptedTextField(keyring=INTEGRATION_KEYS)
    previous_secret_ciphertext = EncryptedTextField(keyring=INTEGRATION_KEYS, null=True, blank=True)
    previous_valid_until = models.DateTimeField(null=True, blank=True)
    is_enabled = models.BooleanField(default=True)
    disabled_at = models.DateTimeField(null=True, blank=True)
    disabled_reason = models.CharField(max_length=64, blank=True, default="")

    class Meta(OrganizationRecord.Meta):
        pass

    def __str__(self):
        return self.name


class WebhookDelivery(OperationalRecord):
    """One event to one endpoint: the fixed body, its attempts and the last outcome (#64).

    Operational pipeline state: no revision, soft delete or per-row audit; only the
    retention job removes rows. ``payload`` carries identifiers only, never record fields.
    """

    class State(models.TextChoices):
        PENDING = "pending", "Na fila"
        RETRYING = "retrying", "Nova tentativa agendada"
        SUCCEEDED = "succeeded", "Entregue"
        FAILED = "failed", "Falhou"
        UNAUTHORIZED = "unauthorized", gettext_lazy("Sem acesso ao registro")
        SKIPPED = "skipped", "Ignorada"

    organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="sk_integrations_delivery_records"
    )
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.PROTECT, related_name="deliveries")
    event_id = models.UUIDField()
    event_type = models.CharField(max_length=100)
    table_key = models.CharField(max_length=128, blank=True, default="")
    record_sysid = models.UUIDField(null=True, blank=True)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    status_code = models.PositiveIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    response_snippet = models.TextField(blank=True, default="")
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    # A manual resend (#65) is a new delivery of the same event; the original never changes.
    resend_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="resends")
    immutable_fields = (
        "endpoint_id",
        "event_id",
        "event_type",
        "table_key",
        "record_sysid",
        "payload",
        "resend_of_id",
    )

    class Meta(OperationalRecord.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "sysid"),
                name="sk_integrations_delivery_org_sysid",
                violation_error_code="unique",
            ),
            models.UniqueConstraint(
                fields=("endpoint", "event_id"),
                condition=models.Q(resend_of__isnull=True),
                name="sk_integrations_delivery_event",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=("pending", "retrying", "succeeded", "failed", "unauthorized", "skipped")),
                name="sk_integrations_delivery_state",
            ),
        ]
        indexes = [models.Index(fields=("organization", "endpoint", "created_at"), name="sk_integrations_delivery_log")]

    def __str__(self):
        return f"{self.event_type} → {self.endpoint_id}"


class WebhookEndpointHealth(OperationalRecord):
    """Consecutive failed attempts of an endpoint; reaching the limit disables it (#64)."""

    organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="sk_integrations_health_records"
    )
    endpoint = models.OneToOneField(WebhookEndpoint, on_delete=models.PROTECT, related_name="health")
    consecutive_failures = models.PositiveIntegerField(default=0)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("endpoint_id",)

    class Meta(OperationalRecord.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "sysid"), name="sk_integrations_health_org_sysid", violation_error_code="unique"
            ),
        ]

    def __str__(self):
        return f"{self.endpoint_id}: {self.consecutive_failures}"
