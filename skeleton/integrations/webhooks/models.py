from django.conf import settings
from django.db import models

from skeleton.common.crypto import EncryptedTextField
from skeleton.common.models import OrganizationRecord

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
