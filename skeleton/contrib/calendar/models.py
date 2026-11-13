from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext, gettext_lazy

from skeleton.common.models import OrganizationRecord, SecuredRecord


def validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(gettext("Fuso IANA desconhecido.")) from None


class Calendar(SecuredRecord):
    """A calendar of the organization (#68); events derive their authorization from it.

    A ``personal`` calendar belongs to one member: its visibility is locked to ``private`` (only
    record grants delegate it), and each member has exactly one default personal calendar.
    A ``shared`` calendar follows the ordinary visibility rules of ``SecuredRecord``.
    """

    class Kind(models.TextChoices):
        PERSONAL = "personal", gettext_lazy("Pessoal")
        SHARED = "shared", gettext_lazy("Compartilhado")

    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.SHARED)
    is_default = models.BooleanField(default=False)
    color = models.CharField(
        max_length=7,
        default="#2563eb",
        validators=[RegexValidator(r"^#[0-9a-fA-F]{6}$", gettext_lazy("Use uma cor #RRGGBB."))],
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

    def clean(self) -> None:
        super().clean()
        if self.kind == self.Kind.PERSONAL:
            if self.visibility != self.Visibility.PRIVATE:
                raise ValidationError(
                    {"visibility": gettext("Um calendário pessoal é sempre privado; delegue por concessão.")}
                )
            loaded = getattr(self, "_loaded_values", {})
            if not self._state.adding and loaded.get("owner_id") not in (None, self.owner_id):
                raise ValidationError({"owner": gettext("Um calendário pessoal não muda de dono.")})

    def __str__(self) -> str:
        return self.name


def new_uid() -> str:
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
        CONFIRMED = "confirmed", gettext_lazy("Confirmado")
        TENTATIVE = "tentative", gettext_lazy("Provisório")
        CANCELLED = "cancelled", gettext_lazy("Cancelado")

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
    # Recurrence (#70): a series master carries the rule; an override row points to its master
    # (``series``) and names the occurrence it replaces (``recurrence_id``, its original start).
    rrule = models.CharField(max_length=500, blank=True)
    rdates = models.JSONField(default=list, blank=True)
    exdates = models.JSONField(default=list, blank=True)
    recurrence_end = models.DateTimeField(null=True, blank=True)
    series = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="overrides")
    recurrence_id = models.DateTimeField(null=True, blank=True)
    is_cancelled = models.BooleanField(default=False)
    immutable_fields = ("calendar_id", "uid", "organizer_id")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "uid"),
                # Overrides share the UID of their master, as RECURRENCE-ID rows do in iCalendar.
                condition=Q(deleted_at__isnull=True, series__isnull=True),
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
            models.CheckConstraint(
                condition=Q(series__isnull=True, recurrence_id__isnull=True)
                | Q(series__isnull=False, recurrence_id__isnull=False, rrule=""),
                name="skeleton_calendar_event_override_shape",
            ),
            models.UniqueConstraint(
                fields=("series", "recurrence_id"),
                condition=Q(deleted_at__isnull=True, series__isnull=False),
                name="skeleton_calendar_event_one_override",
                violation_error_code="unique",
            ),
        ]
        indexes = [
            models.Index(fields=("calendar", "starts_at"), name="skeleton_cal_event_start"),
            # The window queries (#291): series still running, all-day events, and the reminders job.
            models.Index(
                fields=("calendar", "recurrence_end"),
                condition=Q(series__isnull=True, deleted_at__isnull=True),
                name="skeleton_cal_event_rec_end",
            ),
            models.Index(fields=("calendar", "start_date"), name="skeleton_cal_event_day"),
            models.Index(
                fields=("organization", "recurrence_end"),
                condition=Q(series__isnull=True, deleted_at__isnull=True),
                name="skeleton_cal_event_org_end",
            ),
        ]
        ordering = ("starts_at", "start_date", "sysid")

    def clean(self) -> None:
        super().clean()
        try:
            validate_timezone(self.tzid)
        except ValidationError:
            return  # The field validator reports it; the recurrence cannot be computed without a zone.
        if self.all_day:
            if self.start_date is None or self.end_date is None:
                raise ValidationError({"start_date": gettext("Informe as datas do evento de dia inteiro.")})
            if self.end_date <= self.start_date:
                raise ValidationError({"end_date": gettext("O fim (exclusivo) precisa ser depois do início.")})
        else:
            if self.starts_at is None or self.ends_at is None:
                raise ValidationError({"starts_at": gettext("Informe início e fim do evento.")})
            if self.ends_at <= self.starts_at:
                raise ValidationError({"ends_at": gettext("O fim precisa ser depois do início.")})
        from .recurrence import YEARS, _instant

        if self.all_day:
            # Not None: the all_day branch above raised otherwise.
            years, field_name = (self.start_date.year, self.end_date.year), "start_date"  # type: ignore[union-attr]
        else:
            # Not None: the timed branch above raised otherwise.
            years, field_name = (self.starts_at.year, self.ends_at.year), "starts_at"  # type: ignore[union-attr]
        if not all(YEARS[0] <= year <= YEARS[1] for year in years):
            raise ValidationError({field_name: gettext("Data fora do intervalo aceito.")})
        if self.rrule:
            from .recurrence import validate_rule

            self.rrule = validate_rule(self.rrule)
        for name in ("rdates", "exdates"):
            values = getattr(self, name)
            message = gettext("Use uma lista de até 1000 datas ISO 8601.")
            if not isinstance(values, list) or len(values) > 1000:
                raise ValidationError({name: message})
            normalized = []
            for value in values:
                try:
                    moment = _instant(value, self) if isinstance(value, str) else None
                except (ValueError, OverflowError):
                    moment = None
                if moment is None or not YEARS[0] <= moment.year <= YEARS[1]:
                    raise ValidationError({name: message})
                normalized.append(moment.isoformat())  # Stored as ISO 8601 in UTC (#236).
            setattr(self, name, normalized)
        if self.series_id and self.series_id == self.pk:
            raise ValidationError({"series": gettext("Um evento não substitui a si mesmo.")})
        if self.calendar_id and self.organization_id and self.calendar.organization_id != self.organization_id:
            raise ValidationError({"calendar": gettext("O calendário é de outra organização.")})
        if self.series_id is None and (self.starts_at or self.start_date):
            from .recurrence import event_end, series_end

            end = series_end(self)
            if not self._state.adding and self.rrule:
                # An override moved past the rule's last occurrence keeps the series in the window (#294).
                ends = [event_end(row) for row in type(self).objects.filter(series_id=self.pk)]
                end = max([end, *ends]) if end is not None else end
            self.recurrence_end = end

    def __str__(self) -> str:
        return self.title


class EventAttendee(OrganizationRecord):
    """A member of the organization invited to an event (#72); being invited grants reading it."""

    class Response(models.TextChoices):
        NEEDS_ACTION = "needs_action", gettext_lazy("Sem resposta")
        ACCEPTED = "accepted", gettext_lazy("Aceito")
        DECLINED = "declined", gettext_lazy("Recusado")
        TENTATIVE = "tentative", gettext_lazy("Talvez")

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="attendees")
    membership = models.ForeignKey(
        "sk_tenancy.Membership", on_delete=models.PROTECT, related_name="skeleton_calendar_invitations"
    )
    response = models.CharField(max_length=16, choices=Response.choices, default=Response.NEEDS_ACTION)
    optional = models.BooleanField(default=False)
    immutable_fields = ("event_id", "membership_id")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("event", "membership"),
                condition=Q(deleted_at__isnull=True),
                name="skeleton_calendar_attendee_live",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=Q(response__in=("needs_action", "accepted", "declined", "tentative")),
                name="skeleton_calendar_attendee_response",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_id} {self.membership_id}"


class EventReminder(OrganizationRecord):
    """Remind the people of an event ``minutes_before`` each occurrence (#73)."""

    class Channel(models.TextChoices):
        DEFAULT = "default", gettext_lazy("Preferências da pessoa")
        EMAIL = "email", gettext_lazy("Email")
        PUSH = "push", gettext_lazy("Push")

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="reminders")
    minutes_before = models.PositiveIntegerField()
    channel_hint = models.CharField(max_length=16, choices=Channel.choices, default=Channel.DEFAULT)
    immutable_fields = ("event_id",)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=Q(minutes_before__gte=0, minutes_before__lte=40320),
                name="skeleton_calendar_reminder_range",
            ),
            models.UniqueConstraint(
                fields=("event", "minutes_before"),
                condition=Q(deleted_at__isnull=True),
                name="skeleton_calendar_reminder_live",
                violation_error_code="unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_id} -{self.minutes_before}min"
