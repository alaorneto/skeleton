from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy

from skeleton.common.crypto import EncryptedTextField
from skeleton.common.models import OperationalRecord, SystemRecord

INTEGRATION_KEYS = "INTEGRATION_ENCRYPTION_KEYS"

DEFAULT_TYPE = "*"


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
    # Copied at fan-out: the cross-organization inbox reads only this self-discoverable row,
    # since Notification itself is visible only inside its tenant (RLS).
    type_code = models.CharField(max_length=100, default="")
    target_table_key = models.CharField(max_length=128, blank=True, default="")
    target_sysid = models.UUIDField(null=True, blank=True)
    snapshot = models.JSONField(default=dict)
    payload = models.JSONField(default=dict)
    group_key = models.CharField(max_length=200, blank=True, default="")
    count = models.PositiveIntegerField(default=1)
    read_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    # Set when the row joins a daily or weekly digest (#55): it never joins another one.
    digest_sent_at = models.DateTimeField(null=True, blank=True)
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


class UserNotificationPreference(SystemRecord):
    """How one person receives one notification type on one channel (#53).

    ``type_code`` ``*`` is the person's default for every type. Without a row the type's
    ``default_channels`` apply. A mandatory type always reaches the in-app inbox.
    """

    class Channel(models.TextChoices):
        IN_APP = "in_app", gettext_lazy("Na aplicação")
        EMAIL = "email", gettext_lazy("Email")
        PUSH = "push", gettext_lazy("Push no navegador")

    class Mode(models.TextChoices):
        OFF = "off", gettext_lazy("Desligado")
        IMMEDIATE = "immediate", gettext_lazy("Imediato")
        DAILY = "daily", gettext_lazy("Resumo diário")
        WEEKLY = "weekly", gettext_lazy("Resumo semanal")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    type_code = models.CharField(max_length=100)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    mode = models.CharField(max_length=16, choices=Mode.choices)
    # Local hour (the person's time zone) of daily and weekly digests; weekly digests go on Mondays.
    digest_hour = models.PositiveSmallIntegerField(default=8)
    immutable_fields = ("user_id", "type_code", "channel")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("user", "type_code", "channel"),
                condition=Q(deleted_at__isnull=True),
                name="sk_notif_preference_live",
                violation_error_code="unique",
            ),
            models.CheckConstraint(condition=Q(digest_hour__lte=23), name="sk_notif_preference_hour"),
            models.CheckConstraint(
                condition=Q(channel="email") | Q(mode__in=("off", "immediate")),
                name="sk_notif_preference_digest_email",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} {self.type_code} {self.channel}"


class NotificationDelivery(OperationalRecord):
    """One email, digest or push of a notification to one person, with its attempts (#54).

    Operational pipeline state: no revision, soft delete or per-row audit; only the
    retention job removes rows. ``dedupe_key`` makes creation idempotent per organization,
    and ``items`` lists the inbox rows of a digest (identifiers only).
    """

    class Channel(models.TextChoices):
        EMAIL = "email", gettext_lazy("Email")
        DIGEST = "digest", gettext_lazy("Resumo por email")
        PUSH = "push", gettext_lazy("Push no navegador")

    class State(models.TextChoices):
        PENDING = "pending", gettext_lazy("Na fila")
        RETRYING = "retrying", gettext_lazy("Nova tentativa agendada")
        SENT = "sent", gettext_lazy("Enviada")
        SKIPPED = "skipped", gettext_lazy("Ignorada")
        DEAD = "dead", gettext_lazy("Falhou")

    organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, related_name="sk_notifications_delivery_records"
    )
    notification = models.ForeignKey(
        Notification, null=True, blank=True, on_delete=models.PROTECT, related_name="channel_deliveries"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    channel = models.CharField(max_length=16, choices=Channel.choices)
    dedupe_key = models.CharField(max_length=200)
    items = models.JSONField(default=list)
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True, default="")
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("notification_id", "user_id", "channel", "dedupe_key", "items")

    class Meta(OperationalRecord.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=("organization", "sysid"),
                name="sk_notifications_delivery_org_sysid",
                violation_error_code="unique",
            ),
            models.UniqueConstraint(
                fields=("organization", "dedupe_key"),
                name="sk_notifications_delivery_dedupe",
                violation_error_code="unique",
            ),
        ]
        indexes = [models.Index(fields=("organization", "state", "created_at"), name="sk_notif_delivery_state")]

    def __str__(self):
        return f"{self.channel} {self.user_id} {self.state}"


class WebPushSubscription(SystemRecord):
    """One browser's push subscription for one person (#56).

    The endpoint is a capability URL and the keys decrypt the payload: all three are
    encrypted at rest (integration keyring) and redacted in the audit trail.
    ``endpoint_digest`` (SHA-256) finds a subscription without decrypting every row.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    endpoint = EncryptedTextField(keyring=INTEGRATION_KEYS)
    endpoint_digest = models.CharField(max_length=64)
    p256dh = EncryptedTextField(keyring=INTEGRATION_KEYS)
    auth_secret = EncryptedTextField(keyring=INTEGRATION_KEYS)
    user_agent = models.CharField(max_length=200, blank=True, default="")
    last_success_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("user_id", "endpoint_digest")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("endpoint_digest",),
                condition=Q(deleted_at__isnull=True),
                name="sk_notif_push_endpoint_live",
                violation_error_code="unique",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} push"
