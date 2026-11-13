"""Organization pages of the calendar under /o/<org>/apps/skeleton_calendar/ (#71).

Server-rendered month, week, day and list views on a CSS grid, without JavaScript. ``?view=`` and
``?date=`` are validated (anything else is a 400 page). Times are shown in the profile time zone.
The pages read through ``CalendarService``, so an unreadable event is never rendered and private
events show as "Ocupado"; the ICS download (and the feed) read ``policies.readable_events`` with the
same masking.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from skeleton.service_api import Conflict, Forbidden, InvalidInput, NotFound
from skeleton.web.views import tenant_page

from .service_api import CalendarService

if TYPE_CHECKING:
    from uuid import UUID

VIEWS = ("month", "week", "day", "list")
PRESETS = {
    "none": "",
    "daily": "FREQ=DAILY",
    "weekly": "FREQ=WEEKLY",
    "monthly": "FREQ=MONTHLY",
    "yearly": "FREQ=YEARLY",
}


def _zone(user: Any) -> ZoneInfo:
    from skeleton.service_api.identity import IdentityService

    try:
        return ZoneInfo(IdentityService().me(actor=user).timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):  # An unknown stored zone falls back to UTC for display.
        return ZoneInfo("UTC")


def _bad_request(request: HttpRequest, organization_id: UUID, detail: str) -> HttpResponse:
    return render(
        request,
        "skeleton_calendar/bad_request.html",
        {"organization_id": organization_id, "detail": detail},
        status=400,
    )


def _window(view: str, day: date) -> tuple[date, date]:
    if view == "day":
        return day, day + timedelta(days=1)
    if view == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=7)
    if view == "list":
        return day, day + timedelta(days=30)
    first = day.replace(day=1)
    start = first - timedelta(days=first.weekday())
    following = (first + timedelta(days=32)).replace(day=1)
    end = following + timedelta(days=(7 - following.weekday()) % 7)
    return start, end


def _step(view: str, day: date, direction: int) -> date:
    if view == "month":
        target = (day.replace(day=1) + timedelta(days=32 * direction if direction > 0 else -1)).replace(day=1)
        return target
    return day + timedelta(days={"day": 1, "week": 7, "list": 30}[view] * direction)


@tenant_page()
def home(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    view = request.GET.get("view", "week")
    if view not in VIEWS:
        return _bad_request(request, organization_id, gettext("Visão desconhecida: use month, week, day ou list."))
    zone = _zone(request.user)
    raw = request.GET.get("date")
    try:
        day = date.fromisoformat(raw) if raw else datetime.now(zone).date()
    except ValueError:
        return _bad_request(request, organization_id, gettext("Data inválida: use AAAA-MM-DD."))
    if not date(1900, 1, 1) <= day <= date(2200, 12, 31):
        return _bad_request(request, organization_id, gettext("Data fora do intervalo aceito."))
    first, last = _window(view, day)
    service = CalendarService()
    try:
        calendars = service.my_calendars(actor=request.user)
        occurrences = service.upcoming(
            actor=request.user,
            start=datetime.combine(first, time(), tzinfo=zone),
            end=datetime.combine(last, time(), tzinfo=zone),
        )
    except (PermissionDenied, Forbidden):
        raise Http404 from None
    except InvalidInput:
        return _bad_request(request, organization_id, gettext("Há eventos demais neste período; use uma visão menor."))
    days: dict[date, list[Any]] = {first + timedelta(days=index): [] for index in range((last - first).days)}
    for occurrence in occurrences:
        local = occurrence.starts_at.date() if occurrence.all_day else occurrence.starts_at.astimezone(zone).date()
        if local in days:
            days[local].append(
                {
                    "occurrence": occurrence,
                    "start": None if occurrence.all_day else occurrence.starts_at.astimezone(zone),
                    "end": None if occurrence.all_day else occurrence.ends_at.astimezone(zone),
                }
            )
    base = reverse("skeleton_calendar:home", kwargs={"organization_id": organization_id})
    return render(
        request,
        "skeleton_calendar/home.html",
        {
            "organization_id": organization_id,
            "calendars": calendars,
            "view": view,
            "views": VIEWS,
            "day": day,
            "days": sorted(days.items()),
            "zone": str(zone),
            "previous_url": f"{base}?view={view}&date={_step(view, day, -1).isoformat()}",
            "next_url": f"{base}?view={view}&date={_step(view, day, 1).isoformat()}",
            "today_url": f"{base}?view={view}",
            "base_url": base,
        },
    )


class EventForm(forms.Form):
    calendar = forms.ChoiceField(label=gettext_lazy("Calendário"))
    title = forms.CharField(label=gettext_lazy("Título"), max_length=200)
    all_day = forms.BooleanField(label=gettext_lazy("Dia inteiro"), required=False)
    day = forms.DateField(label=gettext_lazy("Data"), widget=forms.DateInput(attrs={"type": "date"}))
    start = forms.TimeField(
        label=gettext_lazy("Início"), required=False, widget=forms.TimeInput(attrs={"type": "time"})
    )
    end = forms.TimeField(label=gettext_lazy("Fim"), required=False, widget=forms.TimeInput(attrs={"type": "time"}))
    location = forms.CharField(label=gettext_lazy("Local"), max_length=300, required=False)
    description = forms.CharField(
        label=gettext_lazy("Descrição"), required=False, widget=forms.Textarea(attrs={"rows": 4})
    )
    is_private = forms.BooleanField(label=gettext_lazy("Privado (os outros veem só Ocupado)"), required=False)
    repeat = forms.ChoiceField(
        label=gettext_lazy("Repetir"),
        choices=(
            ("none", gettext_lazy("Não repete")),
            ("daily", gettext_lazy("Diariamente")),
            ("weekly", gettext_lazy("Semanalmente")),
            ("monthly", gettext_lazy("Mensalmente")),
            ("yearly", gettext_lazy("Anualmente")),
        ),
    )
    count = forms.IntegerField(
        label=gettext_lazy("Quantas vezes"), min_value=1, max_value=1000, initial=10, required=False
    )

    def __init__(self, *args: Any, calendars: Iterable[Any] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["calendar"].choices = [  # type: ignore[attr-defined]  # The calendar field is a ChoiceField.
            (str(item.sysid), item.name) for item in calendars
        ]

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean()  # type: ignore[assignment]  # Form.clean returns cleaned_data.
        if not data.get("all_day") and (not data.get("start") or not data.get("end")):
            raise forms.ValidationError(gettext("Informe início e fim, ou marque dia inteiro."))
        return data

    def event_data(self, zone: ZoneInfo) -> dict[str, Any]:
        data = self.cleaned_data
        values = {
            "title": data["title"],
            "location": data["location"],
            "description": data["description"],
            "is_private": data["is_private"],
            "tzid": str(zone),
        }
        if data["all_day"]:
            values.update(all_day=True, start_date=data["day"], end_date=data["day"] + timedelta(days=1))
        else:
            starts = datetime.combine(data["day"], data["start"], tzinfo=zone)
            values.update(starts_at=starts, ends_at=datetime.combine(data["day"], data["end"], tzinfo=zone))
        if data["repeat"] != "none":
            values["rrule"] = f"{PRESETS[data['repeat']]};COUNT={data.get('count') or 10}"
        return values


# Fields of the service and the model that the form shows under another name (#234).
FIELD_OF = {"starts_at": "start", "ends_at": "end", "start_date": "day", "end_date": "day", "rrule": "repeat"}


def _apply_error(form: forms.Form, error: Exception) -> None:
    """Each message next to its field: InvalidInput carries ``errors``, ValidationError ``message_dict``."""
    if isinstance(error, InvalidInput):
        details = dict(error.errors)
    elif isinstance(error, ValidationError):
        details = dict(getattr(error, "message_dict", {}) or {})
    else:
        form.add_error(None, gettext("O evento não pôde ser salvo."))
        return
    if not details:
        form.add_error(None, gettext("Verifique os campos informados."))
    for field, problems in details.items():
        target = FIELD_OF.get(field, field)
        form.add_error(target if target in form.fields else None, list(problems))


@require_http_methods(["GET", "POST"])
@tenant_page()
@transaction.atomic
def event_new(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    service = CalendarService()
    try:
        calendars = [item for item in service.my_calendars(actor=request.user) if item.writable]  # #344
    except (PermissionDenied, Forbidden):
        raise Http404 from None
    if not calendars:
        raise Http404
    zone = _zone(request.user)
    form = EventForm(request.POST or None, calendars=calendars, initial={"day": datetime.now(zone).date()})
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    service.create_event(
                        actor=request.user, calendar_id=form.cleaned_data["calendar"], data=form.event_data(zone)
                    )
            except (ValidationError, InvalidInput, Forbidden, NotFound) as error:
                _apply_error(form, error)
                status = 400
            else:
                messages.success(request, gettext("Evento criado."))
                day = form.cleaned_data["day"].isoformat()
                return redirect(
                    reverse("skeleton_calendar:home", kwargs={"organization_id": organization_id})
                    + f"?view=week&date={day}"
                )
        else:
            status = 400
    return render(
        request, "skeleton_calendar/event_form.html", {"organization_id": organization_id, "form": form}, status=status
    )


@require_POST
@tenant_page()
@transaction.atomic
def event_cancel(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    try:
        revision = int(request.POST.get("revision", ""))
        CalendarService().cancel_event(actor=request.user, event_id=sysid, expected_revision=revision)
    except (ValueError, NotFound, Forbidden):
        raise Http404 from None
    except Conflict:
        messages.error(request, gettext("O evento mudou. Atualize a página antes de tentar novamente."))
    else:
        messages.success(request, gettext("Evento cancelado."))
    return redirect("skeleton_calendar:home", organization_id=organization_id)


@require_POST
@tenant_page()
@transaction.atomic
def member_access(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    from .services import grant_member_access

    try:
        granted = grant_member_access(actor=request.user)
    except Forbidden:
        raise Http404 from None
    messages.success(
        request,
        gettext("Acesso básico concedido ao papel member.") if granted else gettext("O papel member já tinha acesso."),
    )
    return redirect("skeleton_calendar:home", organization_id=organization_id)


@tenant_page()
def calendar_ics(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    """Download one calendar as .ics (session), masked like the pages."""
    from django.http import HttpResponse

    from .ics import calendar_bytes, calendar_components, capped
    from .policies import readable_calendars, readable_events

    calendar = readable_calendars(request.user).filter(pk=sysid).first()
    if calendar is None:
        raise Http404
    busy_only = request.GET.get("busy") == "1"
    body = calendar_bytes(
        calendar.name,
        capped(
            calendar_components(
                request.user, readable_events(request.user).filter(calendar=calendar), busy_only=busy_only
            )
        ),
    )
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="agenda-{calendar.pk}.ics"'
    response["Cache-Control"] = "no-store"
    return response
