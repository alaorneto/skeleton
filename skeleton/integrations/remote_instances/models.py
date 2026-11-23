from django.db import models

from skeleton.common.crypto import EncryptedTextField
from skeleton.common.models import SystemRecord

REMOTE_INSTANCE_KEYS = "REMOTE_INSTANCE_ENCRYPTION_KEYS"


class RemoteInstanceConnection(SystemRecord):
    """A connection of a central instance to a remote one (#712): its platform API and token.

    The token is encrypted with ``REMOTE_INSTANCE_ENCRYPTION_KEYS`` and never shown. The remote
    ``instance_id`` is pinned on the first successful test; a different one later fails with
    ``remote_instance_mismatch``. Only the organization the platform granted reads it, in jobs.
    """

    label = models.CharField(max_length=200)
    base_url = models.CharField(max_length=300)
    token = EncryptedTextField(keyring=REMOTE_INSTANCE_KEYS)
    expected_instance_id = models.UUIDField(null=True, blank=True)
    # Not named ``organization``: that name turns on tenant semantics for SystemRecord models (E017).
    granted_organization = models.ForeignKey(
        "sk_tenancy.Organization", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    is_active = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=40, blank=True, default="")

    def __str__(self):
        return self.label
