from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, RegexValidator
from django.db import models
from django.db.models import Q

from skeleton.common.models import OrganizationRecord, SecuredRecord


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


def new_uid():
    import uuid

    return f"{uuid.uuid4()}@skeleton"


class Event(OrganizationRecord):
    """An event of a calendar (#69); read and write rights come from the calendar.

    A timed event stores ``starts_at``/``ends_at`` in UTC plus the ``tzid`` it was created in; an
    all-day event stores ``start_date``/``end_date`` with an exclusive end. ``is_private`` hides
    title, description and location from everyone but the organizer and the calendar owner,
    who see the event as "Ocupado". Title, description and location are redacted in the audit.
    """

    class Status(models.TextChoices):
        CONFIRMED = "confirmed", "Confirmado"
        TENTATIVE = "tentative", "Provisório"
        CANCELLED = "cancelled", "Cancelado"

    calendar = models.ForeignKey(Calendar, on_delete=models.PROTECT, related_name="events")
    uid = models.CharField(max_length=255, default=new_uid)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, validators=[MaxLengthValidator(10000)])
    location = models.CharField(max_length=300, blank=True)
    all_day = models.BooleanField(default=False)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    tzid = models.CharField(max_length=64, default="UTC", validators=[validate_timezone])
    is_private = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.CONFIRMED)
    organizer = models.ForeignKey(
        "sk_accounts.User", on_delete=models.PROTECT, related_name="skeleton_calendar_organized_events"
    )
    sequence = models.PositiveIntegerField(default=0)
    immutable_fields = ("calendar_id", "uid", "organizer_id")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "uid"),
                condition=Q(deleted_at__isnull=True),
                name="skeleton_calendar_event_live_uid",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(all_day=False, starts_at__isnull=False, ends_at__isnull=False, start_date__isnull=True)
                    & Q(end_date__isnull=True)
                )
                | (
                    Q(all_day=True, start_date__isnull=False, end_date__isnull=False, starts_at__isnull=True)
                    & Q(ends_at__isnull=True)
                ),
                name="skeleton_calendar_event_time_kind",
            ),
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")) | Q(end_date__gt=models.F("start_date")),
                name="skeleton_calendar_event_ends_after_start",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("confirmed", "tentative", "cancelled")), name="skeleton_calendar_event_status"
            ),
        ]
        indexes = [models.Index(fields=("calendar", "starts_at"), name="skeleton_cal_event_start")]
        ordering = ("starts_at", "start_date", "sysid")

    def clean(self):
        super().clean()
        if self.all_day:
            if self.start_date is None or self.end_date is None:
                raise ValidationError({"start_date": "Informe as datas do evento de dia inteiro."})
            if self.end_date <= self.start_date:
                raise ValidationError({"end_date": "O fim (exclusivo) precisa ser depois do início."})
        else:
            if self.starts_at is None or self.ends_at is None:
                raise ValidationError({"starts_at": "Informe início e fim do evento."})
            if self.ends_at <= self.starts_at:
                raise ValidationError({"ends_at": "O fim precisa ser depois do início."})
        if self.calendar_id and self.organization_id and self.calendar.organization_id != self.organization_id:
            raise ValidationError({"calendar": "O calendário é de outra organização."})

    def __str__(self):
        return self.title
