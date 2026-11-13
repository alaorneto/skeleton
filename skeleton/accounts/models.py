from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext, gettext_lazy

from skeleton.common.crypto import EncryptedTextField
from skeleton.common.models import SystemRecord

from .managers import UserManager


class AccountType(models.TextChoices):
    HUMAN = "human", gettext_lazy("Pessoa")
    SERVICE = "service", gettext_lazy("Conta de serviço")


SERVICE_EMAIL_DOMAIN = "service.invalid"


class User(SystemRecord, AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    auth_version = models.PositiveBigIntegerField(default=1)
    local_password_allowed = models.BooleanField(default=True)
    # A service account acts as a member of exactly one organization and never logs in (#44).
    account_type = models.CharField(max_length=16, choices=AccountType.choices, default=AccountType.HUMAN)
    # Not named ``organization``: that name turns on tenant semantics for SystemRecord models (E017).
    service_organization = models.ForeignKey(
        "sk_tenancy.Organization", null=True, blank=True, on_delete=models.PROTECT, related_name="service_accounts"
    )
    immutable_fields = ("account_type", "service_organization_id")
    groups = None
    user_permissions = None
    objects = UserManager()
    soft_delete_reference_policy = "retain"
    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=Q(deleted_at__isnull=True) | Q(is_active=False), name="sk_accounts_user_deleted_inactive"
            ),
            models.CheckConstraint(condition=Q(auth_version__gte=1), name="sk_accounts_user_auth_version_positive"),
            # A service account belongs to one organization and can never hold a platform flag or a password.
            models.CheckConstraint(
                condition=Q(account_type="human", service_organization__isnull=True)
                | Q(
                    account_type="service",
                    service_organization__isnull=False,
                    is_staff=False,
                    is_superuser=False,
                    local_password_allowed=False,
                ),
                name="sk_accounts_user_service_account",
            ),
        ]

    def __str__(self):
        return (
            gettext("%(email)s (conta de serviço)") % {"email": self.email} if self.is_service_account else self.email
        )

    @property
    def is_service_account(self):
        return self.account_type == AccountType.SERVICE

    def clean_fields(self, exclude=None):
        self.email = UserManager.normalize_email(self.email)
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        self.email = UserManager.normalize_email(self.email)
        if self.deleted_at is not None and self.is_active:
            raise ValidationError({"is_active": gettext("Uma identidade excluída não pode ficar ativa.")})

    def save(self, *args, **kwargs):
        previous_revision = self.revision
        was_adding = self._state.adding
        super().save(*args, **kwargs)
        if self._password is not None and (was_adding or self.revision != previous_revision):
            password_validation.password_changed(self._password, self)
            self._password = None

    def _prepare_soft_delete(self):
        self.is_active = False
        self.auth_version += 1
        return {"is_active", "auth_version"}

    def _prepare_restore(self):
        self.is_active = False
        self.auth_version += 1
        return {"is_active", "auth_version"}

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self):
        return self.first_name or self.email

    # PermissionsMixin short-circuits for any active superuser; the platform predicate decides.
    def has_perm(self, perm, obj=None):
        from skeleton.accounts.platform import is_platform_admin

        return is_platform_admin(self)

    def has_module_perms(self, app_label):
        from skeleton.accounts.platform import is_platform_admin

        return is_platform_admin(self)


class UserProfile(SystemRecord):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="profile")
    display_name = models.CharField(max_length=150, blank=True)
    # Blank follows the organization, then the instance (#170).
    timezone = models.CharField(max_length=64, blank=True, default="")
    language = models.CharField(max_length=16, blank=True, default="")
    immutable_fields = ("user_id",)

    def clean(self):
        super().clean()
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValidationError({"timezone": gettext("Fuso horário inválido.")}) from None
        if self.language and self.language not in {code for code, _ in settings.LANGUAGES}:
            raise ValidationError({"language": gettext("Idioma não suportado.")})

    def __str__(self):
        return self.display_name or str(self.user_id)


class IdentityProvider(SystemRecord):
    """A corporate identity provider bound to one organization, stored in the database (#29).

    Identity (``provider_id``, protocol, organization, issuer/entity/directory, ``client_id``,
    SP entity and the binding fingerprint) is immutable: changing it would silently rebind
    existing ``SocialAccount`` subjects, so a different identity is a new provider. ``settings``
    holds only the allowlisted, non-secret protocol options; ``secret`` and ``private_key`` are
    encrypted with ``IDENTITY_PROVIDER_ENCRYPTION_KEYS`` and redacted in the audit trail.
    The ``SocialApp`` allauth needs is built in memory from this row, never stored.
    """

    class Protocol(models.TextChoices):
        OIDC = "oidc", "OpenID Connect"
        SAML = "saml", "SAML 2.0"
        LDAP = "ldap", "LDAP"

    provider_id = models.CharField(max_length=64, unique=True)
    protocol = models.CharField(max_length=8, choices=Protocol.choices)
    # Not named ``organization``: a global row that points to one organization (E017).
    bound_organization = models.ForeignKey("sk_tenancy.Organization", on_delete=models.PROTECT, related_name="+")
    issuer = models.CharField(max_length=500, blank=True, default="")
    entity_id = models.CharField(max_length=500, blank=True, default="")
    directory = models.CharField(max_length=500, blank=True, default="")
    client_id = models.CharField(max_length=255, blank=True, default="")
    sp_entity_id = models.CharField(max_length=500, blank=True, default="")
    fingerprint = models.CharField(max_length=64)
    name = models.CharField(max_length=200)
    enabled = models.BooleanField(default=True)
    show_on_login = models.BooleanField(default=True)
    trust_verified_email = models.BooleanField(default=False)
    settings = models.JSONField(default=dict, blank=True)
    secret = EncryptedTextField(null=True, blank=True)
    private_key = EncryptedTextField(null=True, blank=True)
    last_check_at = models.DateTimeField(null=True, blank=True)
    last_check_code = models.CharField(max_length=40, blank=True, default="")
    # Directory sync of LDAP providers (#33): counts and codes only, never directory data.
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_code = models.CharField(max_length=40, blank=True, default="")
    immutable_fields = (
        "provider_id",
        "protocol",
        "bound_organization_id",
        "issuer",
        "entity_id",
        "directory",
        "client_id",
        "sp_entity_id",
        "fingerprint",
    )

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(protocol__in=("oidc", "saml", "ldap")), name="sk_accounts_idp_protocol"),
            models.CheckConstraint(
                condition=Q(provider_id__regex=r"^[a-z0-9][a-z0-9_-]{0,63}$"), name="sk_accounts_idp_provider_id"
            ),
            models.CheckConstraint(
                condition=(~Q(protocol="oidc") | (~Q(issuer="") & ~Q(client_id="")))
                & (~Q(protocol="saml") | (~Q(entity_id="") & ~Q(client_id="")))
                & (~Q(protocol="ldap") | ~Q(directory="")),
                name="sk_accounts_idp_identity",
            ),
        ]

    def __str__(self):
        return self.provider_id


class PlatformRoleAssignment(SystemRecord):
    """A limited platform role of one identity (#34): auditor or release operator.

    The full platform administrator stays defined by the ``is_staff``/``is_superuser`` pair;
    these rows add read-only audit and inventory (auditor) or release operations (operator)
    and never an organization's access. Granted and revoked only by a platform administrator,
    bumping the identity's ``auth_version``; revoking is a soft delete.
    """

    class Role(models.TextChoices):
        AUDITOR = "auditor", "Auditor de plataforma"
        OPERATOR = "operator", "Operador de releases"
        BILLING = "billing", "Operador de cobrança"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="platform_roles")
    role = models.CharField(max_length=16, choices=Role.choices)
    immutable_fields = ("user_id", "role")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("user", "role"),
                condition=Q(deleted_at__isnull=True),
                name="sk_accounts_platform_role_live",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=Q(role__in=("auditor", "operator", "billing")), name="sk_accounts_platform_role"
            ),
        ]

    def __str__(self):
        return f"{self.user_id}: {self.role}"


class ErasureRequest(SystemRecord):
    """A person's request to anonymize their identity, decided by a platform administrator (#191).

    ``pending`` → ``completed`` (the approval anonymizes, in its transaction) or ``rejected``;
    the person may withdraw a pending request (``cancelled``). One pending request per person. The
    row keeps only identifiers and dates: it outlives the anonymization as its record.
    """

    class Status(models.TextChoices):
        PENDING = "pending", gettext_lazy("Aguardando aprovação")
        COMPLETED = "completed", gettext_lazy("Concluído")
        REJECTED = "rejected", gettext_lazy("Recusado")
        CANCELLED = "cancelled", gettext_lazy("Retirado")

    OPEN = (Status.PENDING,)

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="erasure_requests")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("user_id",)

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("user",),
                condition=Q(status="pending", deleted_at__isnull=True),
                name="sk_accounts_erasure_open",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("pending", "completed", "rejected", "cancelled")),
                name="sk_accounts_erasure_status",
            ),
        ]

    def __str__(self):
        return f"{self.user_id}: {self.status}"
