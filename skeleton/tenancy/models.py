from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from skeleton.common.models import OrganizationRecord, SystemRecord


class Organization(SystemRecord):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    immutable_fields = ("slug",)
    soft_delete_reference_policy = "retain"

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(deleted_at__isnull=True) | Q(is_active=False), name="sk_tenancy_org_deleted_inactive"),
        ]

    def clean(self):
        super().clean()
        if self.deleted_at is not None and self.is_active:
            raise ValidationError({"is_active": "A deleted organization cannot be active."})

    def _prepare_soft_delete(self):
        self.is_active = False
        return {"is_active"}

    def _prepare_restore(self):
        self.is_active = False
        return {"is_active"}

    def __str__(self):
        return self.name


class Membership(OrganizationRecord):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="organization_memberships")
    is_active = models.BooleanField(default=True)
    immutable_fields = ("user_id",)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(fields=("organization", "user"), name="sk_tenancy_membership_org_user", violation_error_code='unique'),
        ]

    def __str__(self):
        return self.user.email

    def clean(self):
        super().clean()
        if self.deleted_at is None and self.is_active:
            from skeleton.accounts.models import User

            using = self._state.db or "default"
            if self.user_id and not User.all_objects.using(using).filter(pk=self.user_id, is_active=True, deleted_at__isnull=True).exists():
                raise ValidationError({"user": "Membership requires an active identity."})
            if self.organization_id and not Organization.all_objects.using(using).filter(pk=self.organization_id, is_active=True, deleted_at__isnull=True).exists():
                raise ValidationError({"organization": "Membership requires an active organization."})

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
            models.CheckConstraint(condition=(Q(accepted_at__isnull=True, accepted_by__isnull=True) | Q(accepted_at__isnull=False, accepted_by__isnull=False)), name="sk_tenancy_invitation_acceptance"),
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
