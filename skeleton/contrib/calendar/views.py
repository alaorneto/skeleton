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


MANAGED = gettext_lazy("Este evento é gerenciado por outra app; altere-o na origem.")


def _zone(user: Any) -> ZoneInfo:
    from skeleton.service_api.identity import IdentityService

    try:
        return ZoneInfo(IdentityService().me(actor=user).timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):  # An unknown stored zone falls back to UTC for display.
        return ZoneInfo("UTC")


def _bad_request(request: HttpRequest, organization_id: UUID, detail: str, *, status: int = 400) -> HttpResponse:
    return render(
        request,
        "skeleton_calendar/bad_request.html",
        {"organization_id": organization_id, "detail": detail},
        status=status,
    )


def _managed_sources(organization_id: UUID, occurrences: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Name and whether each source app of the managed events is active here (#540), read once."""
    from skeleton.system.applications import application_state

    sources: dict[str, dict[str, Any]] = {}
    for label in {item.source_app for item in occurrences if item.source_app}:
        try:
            state = application_state(label)
        except KeyError:
            sources[label] = {"name": label, "active": False}
            continue
        sources[label] = {"name": state["label"], "active": bool(state["effective"])}
    return sources


def managed_view(organization_id: UUID, occurrence: Any, sources: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The "managed by" mark of an occurrence and its link to the source record, when the app is active."""
    if not occurrence.source_app:
        return None
    source = sources.get(occurrence.source_app, {"name": occurrence.source_app, "active": False})
    url = None
    if source["active"] and occurrence.source_path:
        url = reverse("web:dashboard", kwargs={"organization_id": organization_id}) + occurrence.source_path
    return {"name": source["name"], "url": url}


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
    sources = _managed_sources(organization_id, occurrences)
    for occurrence in occurrences:
        local = occurrence.starts_at.date() if occurrence.all_day else occurrence.starts_at.astimezone(zone).date()
        if local in days:
            days[local].append(
                {
                    "occurrence": occurrence,
                    "start": None if occurrence.all_day else occurrence.starts_at.astimezone(zone),
                    "end": None if occurrence.all_day else occurrence.ends_at.astimezone(zone),
                    "managed": managed_view(organization_id, occurrence, sources),
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
    except Conflict as error:
        if error.code == "managed_event":
            # A managed event (#540) changes only in its source app; a forged POST gets 409.
            return _bad_request(request, organization_id, str(MANAGED), status=409)
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


# --------------------------------------------------------------------------- calendars and sharing (#670)

MANAGED_CALENDAR = gettext_lazy("Este calendário é gerenciado por outra app; altere-o na origem.")
DIRECTORY_PAGES = 5  # Up to 500 groups and 500 people in the sharing choices.


def _can_manage_access(user: Any) -> bool:
    from skeleton.service_api.access import AccessService

    return "access.manage" in AccessService().capabilities(actor=user)


def _can_create_calendar(user: Any) -> bool:
    from skeleton.service_api.access import AccessService

    from .policies import TABLE_KEY

    return _can_manage_access(user) and AccessService().can(actor=user, action="create", table_key=TABLE_KEY)


def _can_delete(user: Any, calendar: Any) -> bool:
    from skeleton.service_api.access import AccessService

    from .policies import TABLE_KEY

    return AccessService().can(actor=user, action="delete", table_key=TABLE_KEY, record_id=calendar.pk)


def _directory(user: Any) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Groups and people of the organization as form choices, through the core service API."""
    from skeleton.service_api.organizations import OrganizationService

    service = OrganizationService()

    def collect(read: Any) -> list[Any]:
        items: list[Any] = []
        cursor = None
        for _ in range(DIRECTORY_PAGES):
            page = read(cursor)
            items.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        return items

    groups = collect(lambda cursor: service.groups(actor=user, cursor=cursor, page_size=100))
    people = collect(lambda cursor: service.members(actor=user, cursor=cursor, page_size=100))
    return (
        [(str(group.sysid), group.name) for group in groups],
        [(str(person.sysid), person.display_name) for person in people],
    )


def _choices_field(label: Any) -> forms.MultipleChoiceField:
    return forms.MultipleChoiceField(label=label, required=False, widget=forms.CheckboxSelectMultiple)


class SharingForm(forms.Form):
    """Groups and people that read or write a shared calendar; writing includes reading."""

    writer_groups = _choices_field(gettext_lazy("Grupos que escrevem"))
    reader_groups = _choices_field(gettext_lazy("Grupos que só leem"))
    writer_users = _choices_field(gettext_lazy("Pessoas que escrevem"))
    reader_users = _choices_field(gettext_lazy("Pessoas que só leem"))

    def __init__(self, *args: Any, groups: list[tuple[str, str]], people: list[tuple[str, str]], **kwargs: Any):
        super().__init__(*args, **kwargs)
        for name in ("writer_groups", "reader_groups"):
            self.fields[name].choices = groups  # type: ignore[attr-defined]  # A MultipleChoiceField.
        for name in ("writer_users", "reader_users"):
            self.fields[name].choices = people  # type: ignore[attr-defined]  # A MultipleChoiceField.

    def sharing(self) -> Any:
        from uuid import UUID

        from .services import Sharing

        def ids(name: str) -> frozenset[UUID]:
            return frozenset(UUID(value) for value in self.cleaned_data[name])

        writer_groups, writer_users = ids("writer_groups"), ids("writer_users")
        return Sharing(
            reader_group_ids=ids("reader_groups") - writer_groups,
            writer_group_ids=writer_groups,
            reader_user_ids=ids("reader_users") - writer_users,
            writer_user_ids=writer_users,
        )


class CalendarForm(SharingForm):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=150)
    owner = forms.ChoiceField(label=gettext_lazy("Responsável"))
    timezone = forms.CharField(label=gettext_lazy("Fuso horário (IANA)"), max_length=64)
    idempotency_key = forms.CharField(widget=forms.HiddenInput, max_length=200)
    field_order = ("name", "owner", "timezone", "writer_groups", "reader_groups", "writer_users", "reader_users")

    def __init__(self, *args: Any, groups: list[tuple[str, str]], people: list[tuple[str, str]], **kwargs: Any):
        super().__init__(*args, groups=groups, people=people, **kwargs)
        self.fields["owner"].choices = people  # type: ignore[attr-defined]  # A ChoiceField.

    def clean_timezone(self) -> str:
        from .models import validate_timezone

        value: str = self.cleaned_data["timezone"].strip()
        validate_timezone(value)
        return value


class RenameForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=150)


def _readable_calendar_row(user: Any, sysid: UUID) -> Any:
    from .policies import readable_calendars

    try:
        calendar = readable_calendars(user).filter(pk=sysid).first()
    except PermissionDenied:
        calendar = None
    if calendar is None:
        raise Http404
    return calendar


def _revision(request: HttpRequest) -> int:
    try:
        return int(request.POST.get("revision", ""))
    except ValueError:
        raise Http404 from None


@require_http_methods(["GET", "POST"])
@tenant_page()
@transaction.atomic
def calendars(request: HttpRequest, organization_id: UUID) -> HttpResponse:
    """The calendars the person reads; whoever has ``access.manage`` also creates shared ones."""
    import uuid

    service = CalendarService()
    try:
        rows = service.calendars(actor=request.user)
    except (PermissionDenied, Forbidden):
        raise Http404 from None
    can_create = _can_create_calendar(request.user)
    if request.method == "POST" and not can_create:
        raise PermissionDenied("access.manage and calendar create are required.")
    form = None
    status = 200
    if can_create:
        groups, people = _directory(request.user)
        initial = {
            "owner": str(request.user.pk),
            "timezone": str(_zone(request.user)),
            "idempotency_key": uuid.uuid4().hex,
        }
        form = CalendarForm(request.POST or None, groups=groups, people=people, initial=initial)
        if request.method == "POST":
            if form.is_valid():
                data, sharing = form.cleaned_data, form.sharing()
                try:
                    with transaction.atomic():
                        created = service.create_shared_calendar(
                            actor=request.user,
                            name=data["name"],
                            owner_user_id=uuid.UUID(data["owner"]),
                            timezone=data["timezone"],
                            writer_group_ids=tuple(sorted(sharing.writer_group_ids, key=str)),
                            reader_group_ids=tuple(sorted(sharing.reader_group_ids, key=str)),
                            writer_user_ids=tuple(sorted(sharing.writer_user_ids, key=str)),
                            reader_user_ids=tuple(sorted(sharing.reader_user_ids, key=str)),
                            idempotency_key=data["idempotency_key"],
                        )
                except (ValidationError, InvalidInput, Forbidden, NotFound) as error:
                    _apply_error(form, error)
                    status = 400
                else:
                    messages.success(request, gettext("Calendário criado."))
                    return redirect(
                        "skeleton_calendar:calendar-detail", organization_id=organization_id, sysid=created.sysid
                    )
            else:
                status = 400
    sources = _managed_sources(organization_id, rows)
    items = [{"calendar": row, "managed": managed_view(organization_id, row, sources)} for row in rows]
    return render(
        request,
        "skeleton_calendar/calendars.html",
        {"organization_id": organization_id, "items": items, "form": form},
        status=status,
    )


@tenant_page()
def calendar_detail(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    """One calendar: its origin, the person's link, and renaming, sharing and deleting when allowed."""
    from .models import Calendar
    from .policies import can_write
    from .services import calendar_sharing

    calendar = _readable_calendar_row(request.user, sysid)
    managed = managed_view(organization_id, calendar, _managed_sources(organization_id, [calendar]))
    editable = not calendar.source_app
    sharing_form = None
    if editable and calendar.kind == Calendar.Kind.SHARED and _can_manage_access(request.user):
        groups, people = _directory(request.user)
        current = calendar_sharing(actor=request.user, calendar=calendar)
        sharing_form = SharingForm(
            groups=groups,
            people=people,
            initial={
                "writer_groups": [str(value) for value in current.writer_group_ids],
                "reader_groups": [str(value) for value in current.reader_group_ids],
                "writer_users": [str(value) for value in current.writer_user_ids],
                "reader_users": [str(value) for value in current.reader_user_ids],
            },
        )
    can_rename = editable and can_write(request.user, calendar)
    can_delete = (
        editable
        and _can_delete(request.user, calendar)
        and not (calendar.kind == Calendar.Kind.PERSONAL and calendar.is_default)
    )
    return render(
        request,
        "skeleton_calendar/calendar_detail.html",
        {
            "organization_id": organization_id,
            "calendar": calendar,
            "managed": managed,
            "rename_form": RenameForm(initial={"name": calendar.name}) if can_rename else None,
            "sharing_form": sharing_form,
            "can_delete": can_delete,
        },
    )


def _managed_conflict(request: HttpRequest, organization_id: UUID, calendar: Any) -> HttpResponse | None:
    """A forged POST on a managed calendar gets 409: it changes only in its source app."""
    if calendar.source_app:
        return _bad_request(request, organization_id, str(MANAGED_CALENDAR), status=409)
    return None


@require_POST
@tenant_page()
@transaction.atomic
def calendar_rename(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    from .services import rename_calendar

    calendar = _readable_calendar_row(request.user, sysid)
    conflict = _managed_conflict(request, organization_id, calendar)
    if conflict is not None:
        return conflict
    form = RenameForm(request.POST)
    if not form.is_valid():
        messages.error(request, gettext("Informe um nome de até 150 caracteres."))
    else:
        try:
            with transaction.atomic():
                rename_calendar(
                    actor=request.user,
                    calendar_id=calendar.pk,
                    name=form.cleaned_data["name"],
                    expected_revision=_revision(request),
                )
        except Conflict:
            messages.error(request, gettext("O calendário mudou. Atualize a página antes de tentar novamente."))
        except ValidationError:
            messages.error(request, gettext("O nome não pôde ser salvo."))
        else:
            messages.success(request, gettext("Calendário renomeado."))
    return redirect("skeleton_calendar:calendar-detail", organization_id=organization_id, sysid=calendar.pk)


@require_POST
@tenant_page()
@transaction.atomic
def calendar_sharing(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    from .services import set_calendar_sharing

    calendar = _readable_calendar_row(request.user, sysid)
    if not _can_manage_access(request.user):
        raise PermissionDenied("access.manage is required.")
    conflict = _managed_conflict(request, organization_id, calendar)
    if conflict is not None:
        return conflict
    groups, people = _directory(request.user)
    form = SharingForm(request.POST, groups=groups, people=people)
    if not form.is_valid():
        messages.error(request, gettext("Escolha grupos e pessoas desta organização."))
    else:
        try:
            with transaction.atomic():
                changed = set_calendar_sharing(actor=request.user, calendar=calendar, sharing=form.sharing())
        except (InvalidInput, ValidationError):
            messages.error(request, gettext("O compartilhamento não pôde ser salvo."))
        else:
            messages.success(
                request, gettext("Compartilhamento salvo.") if changed else gettext("O compartilhamento não mudou.")
            )
    return redirect("skeleton_calendar:calendar-detail", organization_id=organization_id, sysid=calendar.pk)


@require_POST
@tenant_page()
@transaction.atomic
def calendar_delete(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    from .services import delete_calendar

    calendar = _readable_calendar_row(request.user, sysid)
    conflict = _managed_conflict(request, organization_id, calendar)
    if conflict is not None:
        return conflict
    try:
        with transaction.atomic():
            delete_calendar(actor=request.user, calendar_id=calendar.pk, expected_revision=_revision(request))
    except Conflict as error:
        if error.code == "live_references":
            messages.error(request, gettext("O calendário tem eventos; exclua-os antes de excluir o calendário."))
        else:
            messages.error(request, gettext("O calendário mudou. Atualize a página antes de tentar novamente."))
    except ValidationError:
        messages.error(request, gettext("Este calendário não pode ser excluído."))
    else:
        messages.success(request, gettext("Calendário excluído."))
        return redirect("skeleton_calendar:calendars", organization_id=organization_id)
    return redirect("skeleton_calendar:calendar-detail", organization_id=organization_id, sysid=calendar.pk)


@require_http_methods(["GET", "POST"])
@tenant_page()
@transaction.atomic
def calendar_link(request: HttpRequest, organization_id: UUID, sysid: UUID) -> HttpResponse:
    """The person's subscription link to one calendar (#669): issued by POST, shown once."""
    from .feed import issue_calendar_link

    calendar = _readable_calendar_row(request.user, sysid)
    url = None
    if request.method == "POST":
        try:
            with transaction.atomic():
                url = issue_calendar_link(request, calendar)
        except NotFound:
            raise Http404 from None
        except (Forbidden, InvalidInput, ValidationError):
            messages.error(request, gettext("Não foi possível criar o link."))
    response = render(
        request,
        "skeleton_calendar/calendar_link.html",
        {"organization_id": organization_id, "calendar": calendar, "url": url},
        status=201 if url else 200,
    )
    response["Cache-Control"] = "no-store"
    return response
