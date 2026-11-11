from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, RegexValidator
from django.db import models
from django.db.models import Q

from skeleton.common.models import SecuredRecord


def validate_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError("Fuso IANA desconhecido.") from None


class Calendar(SecuredRecord):
    """A calendar of the organization (#68); events derive their authorization from it.

    A ``personal`` calendar belongs to one member: its visibility is locked to ``private`` (only
    record grants delegate it), and each member has exactly one default personal calendar.
    A ``shared`` calendar follows the ordinary visibility rules of ``SecuredRecord``.
    """

    class Kind(models.TextChoices):
        PERSONAL = "personal", "Pessoal"
        SHARED = "shared", "Compartilhado"

    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.SHARED)
    is_default = models.BooleanField(default=False)
    color = models.CharField(
        max_length=7, default="#2563eb", validators=[RegexValidator(r"^#[0-9a-fA-F]{6}$", "Use uma cor #RRGGBB.")]
    )
    timezone = models.CharField(max_length=64, default="UTC", validators=[validate_timezone])
    description = models.TextField(blank=True, validators=[MaxLengthValidator(2000)])
    immutable_fields = ("kind",)

    class Meta(SecuredRecord.Meta):
        constraints = SecuredRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "owner"),
                condition=Q(kind="personal", is_default=True, deleted_at__isnull=True),
                name="skeleton_calendar_one_default_personal",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=("personal", "shared")), name="skeleton_calendar_calendar_kind"
            ),
            models.CheckConstraint(
                condition=~Q(kind="personal") | Q(visibility="private"),
                name="skeleton_calendar_personal_private",
            ),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(kind="personal"), name="skeleton_calendar_default_is_personal"
            ),
        ]
        ordering = ("name", "sysid")

    def clean(self):
        super().clean()
        if self.kind == self.Kind.PERSONAL:
            if self.visibility != self.Visibility.PRIVATE:
                raise ValidationError({"visibility": "Um calendário pessoal é sempre privado; delegue por concessão."})
            loaded = getattr(self, "_loaded_values", {})
            if not self._state.adding and loaded.get("owner_id") not in (None, self.owner_id):
                raise ValidationError({"owner": "Um calendário pessoal não muda de dono."})

    def __str__(self):
        return self.name
