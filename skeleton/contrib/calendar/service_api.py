"""Service API of the calendar (docs/service-api.md): the only module other apps may import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext

from skeleton.access.policies import authorized_queryset
from skeleton.service_api import ServiceAPI, service_method

from .models import Calendar

APP_LABEL = "skeleton_calendar"
MAX_OCCURRENCES_PER_CALL = 5000  # Above it the call is InvalidInput, never cut in silence (#291).
MAX_WINDOW_DAYS = 400


@dataclass(frozen=True, slots=True)
class CalendarInfo:
    sysid: UUID
    name: str
    kind: str
    is_default: bool
    color: str
    timezone: str
    owner_id: UUID
    revision: int


def calendar_info(calendar: Calendar) -> CalendarInfo:
    return CalendarInfo(
        sysid=calendar.pk,
        name=calendar.name,
        kind=calendar.kind,
        is_default=calendar.is_default,
        color=calendar.color,
        timezone=calendar.timezone,
        owner_id=calendar.owner_id,
        revision=calendar.revision,
    )


class CalendarService(ServiceAPI):
    """Calendars and events of the current organization, authorized for the actor."""

    app_label = APP_LABEL

    @service_method(read_only=False, since="0.5.0")
    def my_calendars(self, *, actor: Any) -> tuple[CalendarInfo, ...]:
        """Calendars the actor reads in the current organization, the default personal one first.

        Autorização: TablePermission read em skeleton_calendar.calendar e visibilidade dos registros.
        Erros: Forbidden sem a permissão de tabela; Unavailable com a app desativada.
        Efeitos: cria o calendário pessoal padrão do ator na primeira chamada.
        """
        from .services import ensure_personal_calendar

        ensure_personal_calendar(actor)
        rows = authorized_queryset(Calendar, user=actor, action="read").order_by("-is_default", "name", "sysid")
        return tuple(calendar_info(calendar) for calendar in rows)

    @service_method(read_only=True, since="0.5.0")
    def upcoming(
        self, *, actor: Any, start: datetime, end: datetime, calendar_ids: tuple[UUID, ...] | None = None
    ) -> tuple[EventOccurrence, ...]:
        """Occurrences the actor sees in ``[start, end)`` (at most 400 days), masked.

        Autorização: leitura do calendário ou convite; eventos privados e calendários pessoais de
        outra pessoa sem delegação aparecem só como "Ocupado".
        Erros: InvalidInput para janela inválida ou acima de 400 dias; NotFound para calendário
        ilegível ou de outra organização em ``calendar_ids``.
        Efeitos: nenhum.
        """
        from .policies import readable_events

        start, end = _window(start, end)
        events = readable_events(actor)
        if calendar_ids is not None:
            events = events.filter(calendar_id__in=[_readable_calendar(actor, value).pk for value in calendar_ids])
        return tuple(occurrences_in(actor, events, start, end))

    @service_method(read_only=False, since="0.5.0")
    def create_event(
        self, *, actor: Any, calendar_id: UUID, data: dict, idempotency_key: str | None = None
    ) -> EventInfo:
        """Create an event in a calendar the actor updates; the actor is the organizer.

        Autorização: update no calendário; no calendário pessoal de outra pessoa, só delegação.
        Erros: NotFound para calendário ilegível ou de outra organização; Forbidden se for só
        legível; InvalidInput para horários, fuso ou regra de recorrência inválidos.
        Efeitos: grava o evento na transação do chamador; uma ``idempotency_key`` repetida pelo
        mesmo ator devolve o evento existente.
        """
        from skeleton.service_api import Forbidden

        from . import services
        from .models import Event
        from .policies import can_write

        calendar = _readable_calendar(actor, calendar_id)
        if not can_write(actor, calendar):
            raise Forbidden()
        uid = None
        if idempotency_key is not None:
            uid = _idempotent_uid(actor, idempotency_key)
            existing = Event.objects.filter(uid=uid, series__isnull=True).select_related("calendar").first()
            if existing is not None:
                return event_info(actor, existing)
        event = services.create_event(actor=actor, calendar_id=calendar.pk, data=dict(data), uid=uid)
        return event_info(actor, event)

    @service_method(read_only=False, since="0.5.0")
    def update_event(self, *, actor: Any, event_id: UUID, data: dict, expected_revision: int) -> EventInfo:
        """Change an event the actor writes, with CAS.

        Autorização: update no calendário do evento.
        Erros: NotFound para evento ilegível ou de outra organização; Forbidden se for só legível;
        Conflict com revisão desatualizada; InvalidInput.
        Efeitos: grava a alteração, incrementa ``sequence`` e notifica os participantes.
        """
        from skeleton.service_api import Forbidden

        from . import services
        from .policies import can_write

        event = _readable_event(actor, event_id)
        if not can_write(actor, event.calendar):
            raise Forbidden()
        changed = services.update_event(
            actor=actor, event_id=event.pk, data=dict(data), expected_revision=expected_revision
        )
        return event_info(actor, changed)

    @service_method(read_only=False, since="0.5.0")
    def cancel_event(self, *, actor: Any, event_id: UUID, expected_revision: int) -> EventInfo:
        """Mark an event cancelled, with CAS.

        Autorização: update no calendário do evento.
        Erros: NotFound, Forbidden e Conflict como em update_event.
        Efeitos: grava o estado cancelado e notifica os participantes.
        """
        from skeleton.service_api import Forbidden

        from . import services
        from .policies import can_write

        event = _readable_event(actor, event_id)
        if not can_write(actor, event.calendar):
            raise Forbidden()
        cancelled = services.cancel_event(actor=actor, event_id=event.pk, expected_revision=expected_revision)
        return event_info(actor, cancelled)

    @service_method(read_only=True, since="0.5.0")
    def free_busy(
        self, *, actor: Any, user_ids: tuple[UUID, ...], start: datetime, end: datetime
    ) -> tuple[BusyBlock, ...]:
        """Busy intervals of members in their personal calendars: never titles or details.

        Autorização: TablePermission read em skeleton_calendar.calendar; só pessoas com vínculo
        ativo nesta organização entram no resultado.
        Erros: Forbidden sem a permissão de tabela; InvalidInput para janela ou mais de 50 pessoas.
        Efeitos: nenhum.
        """
        from skeleton.service_api import Forbidden, InvalidInput, as_uuid
        from skeleton.service_api.organizations import OrganizationService

        from .models import Event
        from .policies import readable_calendars

        start, end = _window(start, end)
        if not isinstance(user_ids, tuple) or len(user_ids) > 50:
            raise InvalidInput({"user_ids": [gettext("Informe até 50 pessoas.")]})
        try:
            readable_calendars(actor).exists()  # The actor needs the calendar table at all.
        except PermissionDenied:
            raise Forbidden() from None
        identifiers = tuple(as_uuid(value, field="user_ids") for value in user_ids)
        refs = OrganizationService().member_refs(actor=actor, user_ids=identifiers, include_service_accounts=True)
        members = {ref.user_id for ref in refs}
        if not members:
            return ()
        # Personal calendars of the people, outside the actor's visibility: only times leave.
        # One query for everyone (#291).
        events = Event.objects.filter(
            calendar__owner_id__in=members,
            calendar__kind=Calendar.Kind.PERSONAL,
            status__in=("confirmed", "tentative"),
        )
        blocks = [
            BusyBlock(master.calendar.owner_id, occurrence.starts_at, occurrence.ends_at)
            for master, occurrence in _expanded(events, start, end)
            if occurrence.event.status != "cancelled"
        ]
        blocks.sort(key=lambda block: (str(block.user_id), block.starts_at, block.ends_at))
        return tuple(blocks)


@dataclass(frozen=True, slots=True)
class EventInfo:
    """An event as the actor may see it: a busy block hides title, description and location."""

    sysid: UUID
    calendar_id: UUID
    title: str
    description: str
    location: str
    busy_only: bool
    all_day: bool
    starts_at: datetime | None
    ends_at: datetime | None
    start_date: date | None
    end_date: date | None
    tzid: str
    status: str
    organizer_id: UUID
    revision: int


def event_info(actor: Any, event: Any) -> EventInfo:
    from .policies import BUSY_TITLE, sees_details

    details = sees_details(actor, event)
    return EventInfo(
        sysid=event.pk,
        calendar_id=event.calendar_id,
        title=event.title if details else str(BUSY_TITLE),
        description=event.description if details else "",
        location=event.location if details else "",
        busy_only=not details,
        all_day=event.all_day,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        start_date=event.start_date,
        end_date=event.end_date,
        tzid=event.tzid,
        status=event.status,
        organizer_id=event.organizer_id,
        revision=event.revision,
    )


@dataclass(frozen=True, slots=True)
class EventOccurrence:
    """One occurrence in a window, already masked for the actor."""

    event_id: UUID
    calendar_id: UUID
    recurrence_id: datetime | None
    title: str
    busy_only: bool
    all_day: bool
    starts_at: datetime
    ends_at: datetime
    status: str


@dataclass(frozen=True, slots=True)
class BusyBlock:
    user_id: UUID
    starts_at: datetime
    ends_at: datetime


def _readable_calendar(actor: Any, calendar_id: Any) -> Calendar:
    from skeleton.service_api import NotFound, as_uuid

    from .policies import readable_calendars

    identifier = as_uuid(calendar_id, field="calendar_id")
    calendar = readable_calendars(actor).filter(pk=identifier).first()
    if calendar is None:
        raise NotFound()
    return calendar


def _readable_event(actor: Any, event_id: Any) -> Any:
    from skeleton.service_api import NotFound, as_uuid

    from .policies import readable_events

    event = readable_events(actor).select_related("calendar").filter(pk=as_uuid(event_id, field="event_id")).first()
    if event is None:
        raise NotFound()
    return event


def _window(start: Any, end: Any) -> tuple[datetime, datetime]:
    from skeleton.service_api import InvalidInput, as_utc

    if not isinstance(start, datetime) or not isinstance(end, datetime) or start.tzinfo is None or end.tzinfo is None:
        raise InvalidInput({"start": [gettext("Use datas e horas com fuso.")]})
    start, end = as_utc(start), as_utc(end)
    if end <= start:
        raise InvalidInput({"end": [gettext("O fim precisa ser depois do início.")]})
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise InvalidInput(
            {"end": [gettext("Use uma janela de até %(days)s dias.") % {"days": MAX_WINDOW_DAYS}]}
        )  # Checked before any query, even without events (#291).
    return start, end


def _expanded(events: Any, start: datetime, end: datetime) -> list:
    """(master, occurrence) pairs overlapping the window, with a bounded number of queries (#291).

    A single event that starts after the window is never loaded; the overrides of every master
    come in one query; more than ``MAX_OCCURRENCES_PER_CALL`` occurrences is ``InvalidInput``.
    """
    from django.db.models import Q

    from skeleton.service_api import InvalidInput

    from .models import Event
    from .recurrence import expand

    single = Q(rrule="") & (Q(starts_at__lt=end) | Q(start_date__lte=end.date()))
    masters = list(
        events.filter(series__isnull=True)
        .filter(Q(recurrence_end__gte=start) | Q(recurrence_end__isnull=True))
        .filter(single | ~Q(rrule=""))
        .select_related("calendar")
    )
    overrides: dict = {}
    recurring = [master.pk for master in masters if master.rrule]
    if recurring:
        for row in Event.objects.filter(series_id__in=recurring).select_related("calendar"):
            overrides.setdefault(row.series_id, []).append(row)
    pairs: list = []
    for master in masters:
        expansion = expand(master, start, end, overrides=overrides.get(master.pk, ()))
        if expansion.truncated or len(pairs) + len(expansion.occurrences) > MAX_OCCURRENCES_PER_CALL:
            raise InvalidInput({"window": [gettext("Há ocorrências demais neste período; reduza a janela.")]})
        pairs.extend((master, occurrence) for occurrence in expansion.occurrences)
    return pairs


def occurrences_in(actor: Any, events: Any, start: datetime, end: datetime) -> list[EventOccurrence]:
    """Masked occurrences of ``events`` overlapping the window (masters expanded, overrides applied)."""
    from .policies import actor_view, sees_details

    pairs = _expanded(events, start, end)
    view = actor_view(
        actor,
        calendar_ids={occurrence.event.calendar_id for _, occurrence in pairs},
        event_ids={master.pk for master, _ in pairs},
    )
    rows = []
    for event, occurrence in pairs:
        target = occurrence.event
        details = sees_details(actor, target, view)
        rows.append(
            EventOccurrence(
                event_id=event.pk,
                calendar_id=event.calendar_id,
                recurrence_id=occurrence.recurrence_id,
                title=target.title if details else gettext("Ocupado"),
                busy_only=not details,
                all_day=target.all_day,
                starts_at=occurrence.starts_at,
                ends_at=occurrence.ends_at,
                status=target.status,
            )
        )
    rows.sort(key=lambda row: (row.starts_at, str(row.event_id)))
    return rows


def _idempotent_uid(actor: Any, key: str) -> str:
    import hashlib

    from skeleton.service_api import InvalidInput

    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise InvalidInput({"idempotency_key": [gettext("Chave de 1 a 200 caracteres.")]})
    return hashlib.sha256(f"{actor.pk}:{key}".encode()).hexdigest() + "@idempotent"
