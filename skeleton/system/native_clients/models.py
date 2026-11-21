"""Records of the native client distribution (#606): published feeds, channel policy and sync history."""

from django.db import models
from django.db.models import Q

from skeleton.common.models import SystemRecord

CLIENT_ID = r"^[a-z0-9][a-z0-9._-]{1,63}$"
NAME = r"^[a-z0-9][a-z0-9_-]{0,31}$"


class NativeClientRelease(SystemRecord):
    """One verified feed of a client for one channel and OS, as published by a sync.

    ``feed_sha256`` is the hash the signed envelope covers; the feed, the envelope and every
    listed file sit in the private storage under their SHA-256 and are never rewritten. The feed
    served for a channel and OS is the newest live one (not withdrawn, envelope not expired).
    """

    client_id = models.CharField(max_length=64)
    channel = models.CharField(max_length=32)
    os = models.CharField(max_length=32)
    version = models.CharField(max_length=64)
    tag = models.CharField(max_length=200)
    feed_sha256 = models.CharField(max_length=64)
    envelope_sha256 = models.CharField(max_length=64)
    signed_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    files = models.JSONField(default=list)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = (
        "client_id",
        "channel",
        "os",
        "version",
        "tag",
        "feed_sha256",
        "envelope_sha256",
        "signed_at",
        "expires_at",
        "files",
    )

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("client_id", "channel", "os", "feed_sha256"),
                name="sk_system_native_release_feed",
                violation_error_code="unique",
            ),
            models.CheckConstraint(condition=Q(client_id__regex=CLIENT_ID), name="sk_system_native_release_client"),
            models.CheckConstraint(condition=Q(channel__regex=NAME), name="sk_system_native_release_channel"),
            models.CheckConstraint(condition=Q(os__regex=NAME), name="sk_system_native_release_os"),
        ]
        indexes = [
            models.Index(fields=("client_id", "channel", "os", "signed_at"), name="sk_system_native_release_live")
        ]

    def __str__(self):
        return f"{self.client_id} {self.channel}/{self.os} {self.version}"


class NativeClientChannel(SystemRecord):
    """The policy of a channel: the minimum version a client must run (empty: none)."""

    client_id = models.CharField(max_length=64)
    channel = models.CharField(max_length=32)
    minimum_version = models.CharField(max_length=64, blank=True, default="")
    immutable_fields = ("client_id", "channel")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("client_id", "channel"), name="sk_system_native_channel", violation_error_code="unique"
            ),
        ]

    def __str__(self):
        return f"{self.client_id} {self.channel}"


class NativeClientSync(SystemRecord):
    """One verification outcome of a sync: what was published or why a feed was refused (codes only)."""

    class Outcome(models.TextChoices):
        PUBLISHED = "published", "Publicado"
        REJECTED = "rejected", "Recusado"
        FAILED = "failed", "Falhou"

    client_id = models.CharField(max_length=64)
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    reason_code = models.CharField(max_length=40, blank=True, default="")
    tag = models.CharField(max_length=200, blank=True, default="")
    channel = models.CharField(max_length=32, blank=True, default="")
    os = models.CharField(max_length=32, blank=True, default="")
    version = models.CharField(max_length=64, blank=True, default="")
    immutable_fields = ("client_id", "outcome", "reason_code", "tag", "channel", "os", "version")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=Q(outcome__in=("published", "rejected", "failed")), name="sk_system_native_sync_outcome"
            ),
        ]
        indexes = [models.Index(fields=("client_id", "created_at"), name="sk_system_native_sync_recent")]

    def __str__(self):
        return f"{self.client_id} {self.outcome} {self.reason_code}"
