from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth import password_validation
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from skeleton.common.models import SystemRecord

from .managers import UserManager


class User(SystemRecord, AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    auth_version = models.PositiveBigIntegerField(default=1)
    local_password_allowed = models.BooleanField(default=True)
    groups = None
    user_permissions = None
    objects = UserManager()
    soft_delete_reference_policy = "retain"
    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(deleted_at__isnull=True) | Q(is_active=False), name="sk_accounts_user_deleted_inactive"),
            models.CheckConstraint(condition=Q(auth_version__gte=1), name="sk_accounts_user_auth_version_positive"),
        ]

    def clean_fields(self, exclude=None):
        self.email = UserManager.normalize_email(self.email)
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        self.email = UserManager.normalize_email(self.email)
        if self.deleted_at is not None and self.is_active:
            raise ValidationError({"is_active": "A deleted identity cannot be active."})

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

    def __str__(self):
        return self.email


class UserProfile(SystemRecord):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="profile")
    display_name = models.CharField(max_length=150, blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    language = models.CharField(max_length=16, default="pt-br")
    immutable_fields = ("user_id",)

    def clean(self):
        super().clean()
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError({"timezone": "Unknown IANA time zone."}) from None
        if self.language not in {code for code, _ in settings.LANGUAGES}:
            raise ValidationError({"language": "Unsupported language."})

    def __str__(self):
        return self.display_name or str(self.user_id)
