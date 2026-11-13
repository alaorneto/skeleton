from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext

from skeleton.accounts.emails import normalize_email
from skeleton.common.models import OrganizationRecord, SystemRecord


class Organization(SystemRecord):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    immutable_fields = ("slug",)
    soft_delete_reference_policy = "retain"

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=Q(deleted_at__isnull=True) | Q(is_active=False), name="sk_tenancy_org_deleted_inactive"
            ),
        ]

    def clean(self):
        super().clean()
        if self.deleted_at is not None and self.is_active:
            raise ValidationError({"is_active": gettext("Uma organização excluída não pode ficar ativa.")})

    def _prepare_soft_delete(self):
        self.is_active = False
        return {"is_active"}

    def _prepare_restore(self):
        self.is_active = False
        return {"is_active"}

    def __str__(self):
        return self.name


class Membership(OrganizationRecord):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="organization_memberships"
    )
    is_active = models.BooleanField(default=True)
    immutable_fields = ("user_id",)
    # Frozen as self_discovery_column in sk_tenancy.0003; read across tenants only via own_rows().
    self_discovery_field = "user"

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "user"), name="sk_tenancy_membership_org_user", violation_error_code="unique"
            ),
        ]

    def __str__(self):
        return self.user.email

    def clean(self):
        super().clean()
        if self.deleted_at is None and self.user_id:
            from skeleton.accounts.models import AccountType, User

            using = self._state.db or "default"
            # A service account has exactly one membership: the organization that created it (#44).
            owner = (
                User.all_objects.using(using)
                .filter(pk=self.user_id, account_type=AccountType.SERVICE)
                .values_list("service_organization_id", flat=True)
                .first()
            )
            if owner is not None and owner != self.organization_id:
                raise ValidationError(
                    {
                        "user": ValidationError(
                            gettext("Uma conta de serviço pertence a uma só organização."), code="service_account"
                        )
                    }
                )
        if self.deleted_at is None and self.is_active:
            from skeleton.accounts.models import User

            using = self._state.db or "default"
            if (
                self.user_id
                and not User.all_objects.using(using)
                .filter(pk=self.user_id, is_active=True, deleted_at__isnull=True)
                .exists()
            ):
                raise ValidationError({"user": gettext("O vínculo exige uma identidade ativa.")})
            if (
                self.organization_id
                and not Organization.all_objects.using(using)
                .filter(pk=self.organization_id, is_active=True, deleted_at__isnull=True)
                .exists()
            ):
                raise ValidationError({"organization": gettext("O vínculo exige uma organização ativa.")})

    def _prepare_restore(self):
        # Services choose the requested active state before the single restore CAS.
        return {"is_active"}


class Invitation(OrganizationRecord):
    email = models.EmailField()
    role = models.ForeignKey("sk_access.Role", on_delete=models.PROTECT, related_name="invitations")
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True, editable=False)
    accepted_by = models.UUIDField(null=True, blank=True, editable=False)
    immutable_fields = ("email", "role_id", "token_digest", "expires_at")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=(
                    Q(accepted_at__isnull=True, accepted_by__isnull=True)
                    | Q(accepted_at__isnull=False, accepted_by__isnull=False)
                ),
                name="sk_tenancy_invitation_acceptance",
            ),
        ]

    def clean_fields(self, exclude=None):
        self.email = normalize_email(self.email)
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        self.email = normalize_email(self.email)
        if len(self.token_digest) != 64 or any(char not in "0123456789abcdef" for char in self.token_digest):
            raise ValidationError({"token_digest": "A SHA256 hexadecimal digest is required."})

    def __str__(self):
        return self.email


class OrganizationAdmissionPolicy(OrganizationRecord):
    """How outsiders join the organization (#26); without a row every path is off.

    Changed only by ``skeleton.tenancy.admission.configure_policy`` (``admission.configure``),
    with CAS from the virtual revision 0. Links (#27) and domains (#28) read their own flags.
    """

    discoverable = models.BooleanField(default=False)
    join_requests_enabled = models.BooleanField(default=False)
    request_default_role = models.ForeignKey(
        "sk_access.Role", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    join_links_enabled = models.BooleanField(default=False)
    signup_via_link_enabled = models.BooleanField(default=False)
    domain_join_enabled = models.BooleanField(default=False)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization",), name="sk_tenancy_admission_one_per_org", violation_error_code="unique"
            ),
        ]


class OrganizationDirectoryEntry(SystemRecord):
    """Pre-tenant index of the admission policy (#26): read by ``/join/`` before any tenant exists.

    Written only by ``configure_policy`` in the policy's transaction. Not named
    ``organization``: that name turns on tenant semantics for SystemRecord models (E017).
    """

    listed_organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    discoverable = models.BooleanField(default=False)
    accepts_requests = models.BooleanField(default=False)
    # Admission by verified email domain is on (#28): suggested on the home page.
    accepts_domain = models.BooleanField(default=False)
    immutable_fields = ("listed_organization_id",)

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("listed_organization",), name="sk_tenancy_directory_one_per_org", violation_error_code="unique"
            ),
        ]


class JoinLink(OrganizationRecord):
    """A shareable, revocable multi-use join link (#27).

    Only the SHA-256 of the 32-byte token is stored; the token is shown once on creation.
    Digest, role, mode, validity and limit are immutable; revoking is a soft delete.
    ``direct`` admits a person without a previous membership; ``approval`` (and anyone who was
    a member before) creates a join request instead.
    """

    class Mode(models.TextChoices):
        DIRECT = "direct", "Entrada direta"
        APPROVAL = "approval", "Com aprovação"

    MAX_DAYS = 90
    MAX_USES = 10000

    label = models.CharField(max_length=100)
    token_digest = models.CharField(max_length=64, unique=True, editable=False)
    role = models.ForeignKey("sk_access.Role", on_delete=models.PROTECT, related_name="+")
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.APPROVAL)
    expires_at = models.DateTimeField()
    max_uses = models.PositiveIntegerField()
    use_count = models.PositiveIntegerField(default=0)
    immutable_fields = ("token_digest", "role_id", "mode", "expires_at", "max_uses")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=Q(use_count__gte=0, use_count__lte=models.F("max_uses"), max_uses__gte=1)
                & Q(max_uses__lte=10000),
                name="sk_tenancy_joinlink_uses",
            ),
            models.CheckConstraint(condition=Q(mode__in=("direct", "approval")), name="sk_tenancy_joinlink_mode"),
        ]

    def clean(self):
        super().clean()
        if len(self.token_digest) != 64 or any(char not in "0123456789abcdef" for char in self.token_digest):
            raise ValidationError({"token_digest": "A SHA256 hexadecimal digest is required."})

    def __str__(self):
        return self.label


class JoinRequest(OrganizationRecord):
    """A person asks to join (#26); deciding creates or reactivates the membership.

    The requester reads their own requests in every organization by self-discovery
    (``self_discovery_field``); writes stay in one tenant. ``expired`` is also derived:
    an open request past ``expires_at`` can no longer be decided.
    """

    class Channel(models.TextChoices):
        DIRECTORY = "directory", "Pedido"
        DOMAIN = "domain", "Domínio verificado"
        LINK = "link", "Link de ingresso"
        SSO = "sso", "Login corporativo"

    class Status(models.TextChoices):
        AWAITING_VERIFICATION = "awaiting_verification", "Aguardando confirmação do email"
        PENDING = "pending", "Aguardando aprovação"
        APPROVED = "approved", "Aprovado"
        REJECTED = "rejected", "Recusado"
        WITHDRAWN = "withdrawn", "Retirado"
        EXPIRED = "expired", "Expirado"

    OPEN = ("awaiting_verification", "pending")
    DECIDED = ("approved", "rejected")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="join_requests")
    channel = models.CharField(max_length=16, choices=Channel.choices)
    role = models.ForeignKey("sk_access.Role", on_delete=models.PROTECT, related_name="+")
    join_link = models.ForeignKey(
        "sk_tenancy.JoinLink", on_delete=models.PROTECT, null=True, blank=True, related_name="requests"
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    message = models.CharField(max_length=500, blank=True, default="")
    expires_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.UUIDField(null=True, blank=True)
    immutable_fields = ("user_id", "channel", "join_link_id")
    # Frozen as self_discovery_column in sk_tenancy.0005; read across tenants only via own_rows().
    self_discovery_field = "user"

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "user"),
                condition=Q(status__in=("awaiting_verification", "pending"), deleted_at__isnull=True),
                name="sk_tenancy_joinrequest_one_open",
                violation_error_code="unique",
            ),
            # Decisions carry a time; only decisions carry a decider; a rejection is always human.
            # An approval without decider is automatic (a direct link after email confirmation, #27).
            models.CheckConstraint(
                condition=(~Q(status__in=("approved", "rejected")) | Q(decided_at__isnull=False))
                & (Q(decided_by__isnull=True) | Q(status__in=("approved", "rejected")))
                & (~Q(status="rejected") | Q(decided_by__isnull=False)),
                name="sk_tenancy_joinrequest_decision",
            ),
            models.CheckConstraint(
                condition=~Q(status="withdrawn") | Q(decided_at__isnull=False),
                name="sk_tenancy_joinrequest_withdrawn_at",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=("awaiting_verification", "pending", "approved", "rejected", "withdrawn", "expired")
                )
                & Q(channel__in=("directory", "domain", "link", "sso")),
                name="sk_tenancy_joinrequest_choices",
            ),
        ]

    def __str__(self):
        return f"{self.user_id} -> {self.organization_id}: {self.status}"


class VerifiedDomain(OrganizationRecord):
    """An email domain the organization proves it controls with a DNS TXT record (#28).

    ``challenge`` is public by definition: it is published in ``_skeleton-challenge.<domain>``.
    Only a ``verified`` domain with a live ``DomainClaim`` admits; three failed rechecks in a
    row mark it ``failed`` and drop the claim.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Aguardando verificação"
        VERIFIED = "verified", "Verificado"
        FAILED = "failed", "Falhou"

    class Admission(models.TextChoices):
        AUTO = "auto", "Entrada direta"
        APPROVAL = "approval", "Com aprovação"

    domain = models.CharField(max_length=253)
    challenge = models.CharField(max_length=80)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    admission = models.CharField(max_length=16, choices=Admission.choices, default=Admission.APPROVAL)
    role = models.ForeignKey("sk_access.Role", on_delete=models.PROTECT, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.PositiveSmallIntegerField(default=0)
    last_error_code = models.CharField(max_length=40, blank=True, default="")
    immutable_fields = ("domain", "challenge")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "domain"),
                condition=Q(deleted_at__isnull=True),
                name="sk_tenancy_verifieddomain_live",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("pending", "verified", "failed")) & Q(admission__in=("auto", "approval")),
                name="sk_tenancy_verifieddomain_choices",
            ),
            models.CheckConstraint(
                condition=~Q(status="verified") | Q(verified_at__isnull=False),
                name="sk_tenancy_verifieddomain_verified_at",
            ),
        ]

    def __str__(self):
        return self.domain


class DomainClaim(SystemRecord):
    """Global ownership index of a verified domain (#28), read before any tenant exists.

    Written only by the verification, in the ``VerifiedDomain`` transaction; one live claim
    per domain. Not named ``organization`` (E017).
    """

    domain = models.CharField(max_length=253)
    claiming_organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="+")
    immutable_fields = ("domain", "claiming_organization_id")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("domain",),
                condition=Q(deleted_at__isnull=True),
                name="sk_tenancy_domainclaim_live",
                violation_error_code="unique",
            ),
        ]

    def __str__(self):
        return self.domain


class IdentityProviderAdmission(OrganizationRecord):
    """The organization's admission rules for its own identity provider (#29, #31).

    Without a row JIT provisioning is off. The default role is chosen here, never by a claim,
    query or body; ``allowed_email_domains`` (at most 50, IDNA) narrows who may be provisioned.
    """

    provider = models.ForeignKey("sk_accounts.IdentityProvider", on_delete=models.PROTECT, related_name="+")
    jit_provisioning = models.BooleanField(default=False)
    jit_requires_approval = models.BooleanField(default=False)
    default_role = models.ForeignKey(
        "sk_access.Role", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    allowed_email_domains = models.JSONField(default=list, blank=True)
    immutable_fields = ("provider_id",)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("provider",), name="sk_tenancy_idp_admission_one", violation_error_code="unique"
            ),
        ]

    def clean(self):
        super().clean()
        domains = self.allowed_email_domains
        if not isinstance(domains, list) or len(domains) > 50 or not all(isinstance(item, str) for item in domains):
            raise ValidationError({"allowed_email_domains": "Use uma lista de até 50 domínios."})


BRAND_COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"


class OrganizationSettings(OrganizationRecord):
    """Defaults and brand of one organization (#167), edited by its administrator.

    Blank values fall back to the instance: no display name shows ``Organization.name``, no
    language or time zone uses the instance default and no colour keeps the core theme.
    ``logo_key`` is the private storage key of the logo (#168), never shown and redacted in audit.
    """

    display_name = models.CharField(max_length=200, blank=True, default="")
    default_language = models.CharField(max_length=16, blank=True, default="")
    default_timezone = models.CharField(max_length=64, blank=True, default="")
    brand_color = models.CharField(max_length=7, blank=True, default="")
    logo_key = models.CharField(max_length=300, blank=True, default="")
    # The business calendar (#416): weekday -> [["09:00", "18:00"], ...] and ISO holiday dates.
    business_hours = models.JSONField(default=dict, blank=True)
    holidays = models.JSONField(default=list, blank=True)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization",), name="sk_tenancy_settings_one_per_org", violation_error_code="unique"
            ),
            models.CheckConstraint(
                condition=Q(brand_color="") | Q(brand_color__regex=BRAND_COLOR_PATTERN),
                name="sk_tenancy_settings_brand_color",
            ),
        ]

    def clean(self):
        super().clean()
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if self.default_language and self.default_language not in dict(settings.LANGUAGES):
            raise ValidationError({"default_language": gettext("Idioma não suportado.")})
        if self.default_timezone:
            try:
                ZoneInfo(self.default_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValidationError({"default_timezone": gettext("Fuso horário inválido.")}) from None
        if self.brand_color:
            import re

            if not re.fullmatch(BRAND_COLOR_PATTERN, self.brand_color):
                raise ValidationError({"brand_color": gettext("Use uma cor no formato #RRGGBB.")})

    def __str__(self):
        return self.display_name or str(self.organization_id)
