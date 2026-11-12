from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext

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
        self.email = self.email.strip().lower()
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        self.email = self.email.strip().lower()
        if len(self.token_digest) != 64 or any(char not in "0123456789abcdef" for char in self.token_digest):
            raise ValidationError({"token_digest": "A SHA256 hexadecimal digest is required."})

    def __str__(self):
        return self.email


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
