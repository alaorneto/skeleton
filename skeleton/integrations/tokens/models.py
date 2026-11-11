from django.conf import settings
from django.db import models
from django.db.models import Q

from skeleton.common.models import OperationalRecord, SystemRecord


class ApiToken(SystemRecord):
    """A Bearer credential for ``/api/v1/`` (#45): only the SHA-256 of the secret is stored.

    The token text is ``skt_<prefix>_<secret>``. ``prefix`` (8 characters, unique) finds the
    row; the secret (43 characters, 256 bits) is compared by digest in constant time and is
    shown once, at issue. A token is valid while not expired, not revoked, its user active
    and the user's ``auth_version`` unchanged since issue. ``scopes`` only narrow the user's
    role, never widen it.
    """

    class Kind(models.TextChoices):
        PERSONAL = "personal", "Pessoal"
        SERVICE = "service", "Conta de serviço"
        FEED = "feed", "Feed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="sk_api_tokens")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # Service tokens are bound to the account's organization; a personal token may be bound to one.
    bound_organization = models.ForeignKey(
        "sk_tenancy.Organization", on_delete=models.PROTECT, null=True, blank=True, related_name="sk_api_tokens"
    )
    name = models.CharField(max_length=150)
    prefix = models.CharField(max_length=8)
    digest = models.CharField(max_length=64)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    auth_version_at_issue = models.PositiveIntegerField()
    immutable_fields = (
        "user_id",
        "kind",
        "bound_organization_id",
        "prefix",
        "digest",
        "scopes",
        "expires_at",
        "auth_version_at_issue",
    )

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("prefix",), name="sk_integrations_apitoken_prefix", violation_error_code="unique"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=("personal", "service", "feed")), name="sk_integrations_apitoken_kind"
            ),
            models.CheckConstraint(
                condition=~Q(kind="service") | Q(bound_organization__isnull=False),
                name="sk_integrations_apitoken_service_bound",
            ),
        ]

    def __str__(self):
        return f"skt_{self.prefix}_… ({self.name})"


class ApiTokenActivity(OperationalRecord):
    """Last use of a token (#48), written at most once per window: operational, never audited."""

    token = models.OneToOneField(ApiToken, on_delete=models.PROTECT, related_name="activity")
    last_used_at = models.DateTimeField()
    immutable_fields = ("token_id",)

    def __str__(self):
        return f"{self.token_id} {self.last_used_at}"
