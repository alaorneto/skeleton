from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext, gettext_lazy

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
