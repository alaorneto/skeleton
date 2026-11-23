from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy

from skeleton.common.models import OperationalRecord, SystemRecord


class ApiToken(SystemRecord):
    """A Bearer credential for ``/api/v1/`` (#45): only the SHA-256 of the secret is stored.

    The token text is ``skt_<prefix>_<secret>``. ``prefix`` (8 characters, unique) finds the
    row; the secret (43 characters, 256 bits) is compared by digest in constant time and is
    shown once, at issue. A token is valid while not expired, not revoked, its user active
    and the user's ``auth_version`` unchanged since issue. ``scopes`` only narrow the user's
    role, never widen it.

    A feed token may be bound to one record (#669): ``target_table`` and ``target_id`` name it
    and ``bound_organization`` is its organization. Such a token opens only the feed of that
    record, never the person's feed nor another record's.
    """

    class Kind(models.TextChoices):
        PERSONAL = "personal", gettext_lazy("Pessoal")
        SERVICE = "service", gettext_lazy("Conta de serviço")
        FEED = "feed", gettext_lazy("Feed")
        # The platform API (#709): only for platform clients, only ``platform.*`` scopes.
        PLATFORM = "platform", gettext_lazy("Plataforma")

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
    target_table = models.CharField(max_length=100, blank=True, default="")
    target_id = models.UUIDField(null=True, blank=True)
    # Networks a platform token may come from (#709): CIDRs; empty is any address.
    allowed_networks = models.JSONField(default=list, blank=True)
    immutable_fields = (
        "user_id",
        "kind",
        "bound_organization_id",
        "prefix",
        "digest",
        "scopes",
        "expires_at",
        "auth_version_at_issue",
        "target_table",
        "target_id",
        "allowed_networks",
    )

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("prefix",), name="sk_integrations_apitoken_prefix", violation_error_code="unique"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=("personal", "service", "feed", "platform")),
                name="sk_integrations_apitoken_kind",
            ),
            models.CheckConstraint(
                condition=~Q(kind="platform") | Q(bound_organization__isnull=True, target_table=""),
                name="sk_integrations_apitoken_platform_global",
            ),
            models.CheckConstraint(
                condition=~Q(kind="service") | Q(bound_organization__isnull=False),
                name="sk_integrations_apitoken_service_bound",
            ),
            # A record feed (#669): table, record and organization together, on feed tokens only.
            models.CheckConstraint(
                condition=Q(target_table="", target_id__isnull=True)
                | (Q(kind="feed", target_id__isnull=False, bound_organization__isnull=False) & ~Q(target_table="")),
                name="sk_integrations_apitoken_target",
            ),
        ]

    def __str__(self):
        return f"skt_{self.prefix}_… ({self.name})"

    @property
    def audit_source(self):
        """The GUC ``skeleton.source`` of a request made with this token (never the secret)."""
        return f"http:token:{self.prefix}"


class ApiTokenActivity(OperationalRecord):
    """Last use of a token (#48), written at most once per window: operational, never audited."""

    token = models.OneToOneField(ApiToken, on_delete=models.PROTECT, related_name="activity")
    last_used_at = models.DateTimeField()
    immutable_fields = ("token_id",)

    def __str__(self):
        return f"{self.token_id} {self.last_used_at}"
