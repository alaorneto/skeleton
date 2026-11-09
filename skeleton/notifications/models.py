from django.conf import settings
from django.db import models
from django.db.models import Q

from skeleton.common.models import OperationalRecord


class Notification(OperationalRecord):
    """One notify() call: what happened, about which target, for which recipient specification.

    ``snapshot`` keeps only declared, readable and non-sensitive fields of the
    target; it may outlive the reader's access until retention (accepted risk).
    """

    organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="sk_notifications_notification_records"
    )
    type_code = models.CharField(max_length=100)
    actor_id = models.UUIDField(null=True, blank=True)
    target_table_key = models.CharField(max_length=128, blank=True, default="")
    target_sysid = models.UUIDField(null=True, blank=True)
    payload = models.JSONField(default=dict)
    snapshot = models.JSONField(default=dict)
    recipients = models.JSONField(default=dict)
    group_key = models.CharField(max_length=200, blank=True, default="")
    dedupe_key = models.CharField(max_length=200, null=True, blank=True)  # noqa: DJ001 - NULL means "no key"
    fanned_out_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = (
        "type_code",
        "actor_id",
        "target_table_key",
        "target_sysid",
        "payload",
        "snapshot",
        "recipients",
        "group_key",
        "dedupe_key",
    )

    class Meta(OperationalRecord.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "sysid"),
                name="sk_notifications_notification_org_sysid",
                violation_error_code="unique",
            ),
            models.UniqueConstraint(
                fields=("organization", "type_code", "dedupe_key"),
                condition=Q(dedupe_key__isnull=False),
                name="sk_notifications_notification_dedupe",
                violation_error_code="unique",
            ),
        ]

    def __str__(self):
        return self.type_code


class NotificationRecipient(OperationalRecord):
    """A person's copy of a notification: read and archived markers and the grouped count."""

    organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="sk_notifications_recipient_records"
    )
    notification = models.ForeignKey(Notification, on_delete=models.PROTECT, related_name="deliveries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    group_key = models.CharField(max_length=200, blank=True, default="")
    count = models.PositiveIntegerField(default=1)
    read_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("user_id",)
    # Frozen as self_discovery_column: a person lists their own rows of every organization.
    self_discovery_field = "user"

    class Meta(OperationalRecord.Meta):
        indexes = [
            models.Index(
                fields=("user",),
                condition=Q(read_at__isnull=True, archived_at__isnull=True),
                name="sk_notif_recipient_unread",
            ),
            models.Index(fields=("organization", "user", "created_at"), name="sk_notif_recipient_inbox"),
        ]

    def __str__(self):
        return f"{self.user_id} {self.notification_id}"
