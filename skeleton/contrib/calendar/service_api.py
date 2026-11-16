"""Service API of the calendar (docs/service-api.md): the only module other apps may import."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext

from skeleton.access.policies import authorized_queryset
from skeleton.service_api import ServiceAPI, service_method
from skeleton.tenancy.context import current_context

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
    writable: bool = False  # Whether create_event would accept this calendar for the actor (#344).


def calendar_info(calendar: Calendar, *, writable: bool = False) -> CalendarInfo:
    return CalendarInfo(
        sysid=calendar.pk,
        name=calendar.name,
        kind=calendar.kind,
        is_default=calendar.is_default,
        color=calendar.color,
        timezone=calendar.timezone,
        owner_id=calendar.owner_id,
        revision=calendar.revision,
        writable=writable,
    )


class CalendarService(ServiceAPI):
    """Calendars and events of the current organization, authorized for the actor."""

    app_label = APP_LABEL

    @service_method(read_only=False, since="0.5.0")
    def my_calendars(self, *, actor: Any) -> tuple[CalendarInfo, ...]:
        """Calendars the actor reads in the current organization, the default personal one first.

        Autorização: TablePermission read em skeleton_calendar.calendar e visibilidade dos registros.
        Erros: Forbidden sem a permissão de tabela; Unavailable com a app desativada.
        Efeitos: cria o calendário pessoal padrão do ator na primeira chamada, para qualquer ator
        com TablePermission de leitura, inclusive conta de serviço.
        """
        from .services import ensure_personal_calendar

        ensure_personal_calendar(actor)
        from .policies import can_write

        rows = authorized_queryset(Calendar, user=actor, action="read").order_by("-is_default", "name", "sysid")
        return tuple(calendar_info(calendar, writable=can_write(actor, calendar)) for calendar in rows)

    @service_method(read_only=True, since="0.6.0")
    def calendars(self, *, actor: Any) -> tuple[CalendarInfo, ...]:
        """Every calendar the actor reads in the current organization, without creating any (#539).

        Autorização: TablePermission read em skeleton_calendar.calendar e visibilidade dos registros.
        Erros: Forbidden sem a permissão de tabela; Unavailable com a app desativada.
        Efeitos: nenhum (``my_calendars`` é quem cria o calendário pessoal padrão).
        """
        from skeleton.service_api import Forbidden

        from .policies import can_write

        try:
            rows = authorized_queryset(Calendar, user=actor, action="read").order_by("-is_default", "name", "sysid")
            return tuple(calendar_info(calendar, writable=can_write(actor, calendar)) for calendar in rows)
        except PermissionDenied:
            raise Forbidden() from None

    @service_method(read_only=False, since="0.6.0")
    def create_shared_calendar(
        self,
        *,
        actor: Any,
        name: str,
        owner_user_id: UUID,
        timezone: str,
        writer_group_ids: tuple[UUID, ...] = (),
        reader_group_ids: tuple[UUID, ...] = (),
        idempotency_key: str,
    ) -> CalendarInfo:
        """Create a private shared calendar owned by a member, with writing delegated to groups (#539).

        For example the calendar of the appointments of one professional, written by the support
        team: the groups in ``writer_group_ids`` get ``read`` and ``update``, those in
        ``reader_group_ids`` only ``read``, by record grants of the core.
        Autorização: ``create`` em skeleton_calendar.calendar e a capacidade ``access.manage``
        (a gestão de acesso do núcleo).
        Erros: Forbidden sem uma das duas; InvalidInput para nome, fuso, dono que não é membro ativo,
        grupo de outra organização, mais de 20 grupos ou chave inválida.
        Efeitos: cria o calendário e as concessões na transação do chamador; a mesma
        ``idempotency_key`` do mesmo ator devolve o calendário já criado.
        """
        import uuid

        from skeleton.access.capabilities import ACCESS_MANAGE, has_capability
        from skeleton.access.policies import has_table_permission
        from skeleton.service_api import Forbidden, InvalidInput, as_uuid
        from skeleton.service_api.access import AccessService
        from skeleton.service_api.organizations import OrganizationService

        from .policies import TABLE_KEY

        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise InvalidInput({"idempotency_key": [gettext("Chave de 1 a 200 caracteres.")]})
        if not has_table_permission(Calendar, user=actor, action="create") or not has_capability(actor, ACCESS_MANAGE):
            raise Forbidden()
        groups = {"update": writer_group_ids, "read": reader_group_ids}
        if (
            not all(isinstance(value, tuple) for value in groups.values())
            or len(writer_group_ids) + len(reader_group_ids) > 20
        ):
            raise InvalidInput({"writer_group_ids": [gettext("Informe até 20 grupos.")]})
        organization_id = current_context().organization_id
        sysid = uuid.uuid5(
            uuid.NAMESPACE_URL, f"skeleton-calendar:shared:{organization_id}:{actor.pk}:{idempotency_key}"
        )
        existing = Calendar.objects.filter(pk=sysid).first()
        if existing is not None:
            return calendar_info(existing, writable=True)
        owner = as_uuid(owner_user_id, field="owner_user_id")
        if not OrganizationService().member_refs(actor=actor, user_ids=(owner,)):
            raise InvalidInput({"owner_user_id": [gettext("O dono precisa ser membro ativo desta organização.")]})
        with transaction.atomic(using=current_context().using):
            calendar = Calendar(  # type: ignore[misc]  # The organization is set: this runs in a tenant context.
                sysid=sysid,
                organization_id=organization_id,
                owner_id=owner,
                visibility=Calendar.Visibility.PRIVATE,
                kind=Calendar.Kind.SHARED,
                name=name,
                timezone=timezone,
            )
            calendar.save()
            access = AccessService()
            for action, group_ids in groups.items():
                for group_id in dict.fromkeys(group_ids):
                    for granted in ("read", "update") if action == "update" else ("read",):
                        access.grant_record_access(
                            actor=actor, table_key=TABLE_KEY, record_id=calendar.pk, action=granted, group_id=group_id
                        )
        return calendar_info(calendar, writable=True)

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
        self, *, actor: Any, calendar_id: UUID, data: dict[str, Any], idempotency_key: str | None = None
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
        from .policies import can_write

        calendar = _readable_calendar(actor, calendar_id)
        if not can_write(actor, calendar):
            raise Forbidden()
        uid = None
        if idempotency_key is not None:
            uid = _idempotent_uid(actor, idempotency_key)
            existing = _idempotent_event(uid)
            if existing is not None:
                return event_info(actor, existing)
        try:
            with transaction.atomic(using=current_context().using):
                event = services.create_event(actor=actor, calendar_id=calendar.pk, data=dict(data), uid=uid)
        except (IntegrityError, ValidationError):
            # A concurrent call with the same key created it between the lookup and the insert (#344).
            existing = _idempotent_event(uid) if uid else None
            if existing is None:
                raise
            return event_info(actor, existing)
        return event_info(actor, event)

    @service_method(read_only=False, since="0.5.0")
    def update_event(self, *, actor: Any, event_id: UUID, data: dict[str, Any], expected_revision: int) -> EventInfo:
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

    # ----------------------------------------------------------- events and occurrences (#344)

    @service_method(read_only=True, since="0.6.0")
    def event(self, *, actor: Any, event_id: UUID) -> EventInfo:
        """One event as the actor may see it (a busy block for what it only sees as "Ocupado").

        Autorização: leitura do calendário ou convite.
        Erros: NotFound para evento inexistente, ilegível ou de outra organização.
        Efeitos: nenhum.
        """
        return event_info(actor, _readable_event(actor, event_id))

    @service_method(read_only=False, since="0.6.0")
    def delete_event(self, *, actor: Any, event_id: UUID, expected_revision: int) -> None:
        """Soft delete an event with its reminders, attendees and overrides, with CAS.

        Autorização: update no calendário do evento.
        Erros: NotFound; Forbidden se for só legível; Conflict("stale_revision").
        Efeitos: exclui logicamente as linhas e notifica o cancelamento aos participantes.
        """
        from . import services

        event = _writable_event(actor, event_id)
        services.delete_event(actor=actor, event_id=event.pk, expected_revision=expected_revision)

    @service_method(read_only=False, since="0.6.0")
    def edit_occurrence(
        self,
        *,
        actor: Any,
        event_id: UUID,
        recurrence_id: datetime,
        mode: str,
        data: dict[str, Any],
        expected_revision: int,
    ) -> EventInfo:
        """Change one occurrence (``this``), this and the following ones or the whole series (``all``).

        Autorização: update no calendário do evento.
        Erros: NotFound; Forbidden; Conflict("stale_revision") com a revisão da série desatualizada;
        InvalidInput para modo, instante sem fuso ou ocorrência que a série não tem.
        Efeitos: grava a substituição, a divisão ou o mestre.
        """
        from . import services

        event = _writable_event(actor, event_id)
        changed = services.edit_occurrence(
            actor=actor,
            event_id=event.pk,
            recurrence_id=_aware(recurrence_id),
            mode=mode,
            data=dict(data),
            expected_revision=expected_revision,
        )
        return event_info(actor, changed)

    @service_method(read_only=False, since="0.6.0")
    def cancel_occurrence(
        self, *, actor: Any, event_id: UUID, recurrence_id: datetime, expected_revision: int
    ) -> EventInfo:
        """Remove one occurrence of a series (EXDATE), with CAS.

        Autorização: update no calendário do evento.
        Erros: NotFound; Forbidden; Conflict("stale_revision"); InvalidInput.
        Efeitos: acrescenta o EXDATE ao mestre.
        """
        from . import services

        event = _writable_event(actor, event_id)
        master = services.cancel_occurrence(
            actor=actor, event_id=event.pk, recurrence_id=_aware(recurrence_id), expected_revision=expected_revision
        )
        return event_info(actor, master)

    # ----------------------------------------------------------- attendees (#344)

    @service_method(read_only=True, since="0.6.0")
    def attendees(self, *, actor: Any, event_id: UUID) -> tuple[AttendeeInfo, ...]:
        """Invitations of an event (at most 500); empty for an event the actor only sees as "Ocupado".

        Autorização: leitura do evento.
        Erros: NotFound para evento inexistente, ilegível ou de outra organização.
        Efeitos: nenhum.
        """
        from .models import EventAttendee
        from .policies import sees_details

        event = _readable_event(actor, event_id)
        if not sees_details(actor, event):
            return ()
        rows = (
            EventAttendee.objects.filter(event_id=event.series_id or event.pk)
            .select_related("membership")
            .order_by("created_at", "sysid")[:MAX_LIST]
        )
        return tuple(attendee_info(row) for row in rows)

    @service_method(read_only=False, since="0.6.0")
    def invite(
        self, *, actor: Any, event_id: UUID, user_ids: tuple[UUID, ...], optional: bool = False
    ) -> tuple[AttendeeInfo, ...]:
        """Invite active people of the organization; existing invitations are kept (only the new ones return).

        Autorização: update no calendário do evento.
        Erros: NotFound; Forbidden; InvalidInput para mais de 100 pessoas, pessoa de outra organização,
        inativa ou conta de serviço.
        Efeitos: grava os convites e notifica os convidados novos.
        """
        from skeleton.service_api import InvalidInput, as_uuid

        from . import attendees

        event = _writable_event(actor, event_id)
        if not isinstance(user_ids, tuple) or not 1 <= len(user_ids) <= MAX_INVITES:
            raise InvalidInput({"user_ids": [gettext("Informe de 1 a %(value1)s pessoas.") % {"value1": MAX_INVITES}]})
        identifiers = tuple(as_uuid(value, field="user_ids") for value in user_ids)
        created = attendees.invite(actor=actor, event_id=event.pk, user_ids=identifiers, optional=bool(optional))
        return tuple(attendee_info(row) for row in created)

    @service_method(read_only=False, since="0.6.0")
    def remove_attendee(self, *, actor: Any, attendee_id: UUID, expected_revision: int) -> None:
        """Remove an invitation, with CAS; the person stops reading the event.

        Autorização: update no calendário do evento.
        Erros: NotFound para convite inexistente ou de evento ilegível (a mesma resposta); Forbidden
        se o evento for só legível; Conflict("stale_revision").
        Efeitos: exclui logicamente o convite.
        """
        from . import attendees
        from .models import EventAttendee

        attendee = EventAttendee.objects.filter(pk=_identifier(attendee_id, "attendee_id")).first()
        if attendee is None:
            raise _not_found()
        _writable_event(actor, attendee.event_id)
        attendees.remove(actor=actor, attendee_id=attendee.pk, expected_revision=expected_revision)

    @service_method(read_only=False, since="0.6.0")
    def respond(self, *, actor: Any, event_id: UUID, response: str, expected_revision: int) -> AttendeeInfo:
        """RSVP of the actor's own invitation, with CAS.

        Autorização: leitura do evento; só o convite do próprio ator.
        Erros: NotFound para evento ilegível ou sem convite do ator (igual a inexistente);
        Conflict("stale_revision") com a revisão do convite; InvalidInput para resposta inválida.
        Efeitos: grava a resposta.
        """
        from . import attendees
        from .models import EventAttendee

        event = _readable_event(actor, event_id)
        target = event.series_id or event.pk
        own = EventAttendee.objects.filter(event_id=target, membership__user_id=actor.pk, membership__is_active=True)
        if not own.exists():
            raise _not_found()
        attendee = attendees.respond(
            actor=actor, event_id=target, response=response, expected_revision=expected_revision
        )
        return attendee_info(EventAttendee.objects.select_related("membership").get(pk=attendee.pk))

    # ----------------------------------------------------------- reminders (#344)

    @service_method(read_only=True, since="0.6.0")
    def reminders(self, *, actor: Any, event_id: UUID) -> tuple[ReminderInfo, ...]:
        """Reminders of an event; empty for an event the actor only sees as "Ocupado".

        Autorização: leitura do evento.
        Erros: NotFound para evento inexistente, ilegível ou de outra organização.
        Efeitos: nenhum.
        """
        from .models import EventReminder
        from .policies import sees_details

        event = _readable_event(actor, event_id)
        if not sees_details(actor, event):
            return ()
        rows = EventReminder.objects.filter(event=event).order_by("minutes_before", "sysid")[:MAX_LIST]
        return tuple(reminder_info(row) for row in rows)

    @service_method(read_only=False, since="0.6.0")
    def add_reminder(self, *, actor: Any, event_id: UUID, minutes_before: int) -> ReminderInfo:
        """Add a reminder (0 to 40320 minutes); an existing one with the same lead is returned.

        Autorização: update no calendário do evento.
        Erros: NotFound; Forbidden; InvalidInput para antecedência fora da faixa.
        Efeitos: grava o lembrete.
        """
        from . import reminders

        event = _writable_event(actor, event_id)
        return reminder_info(reminders.add_reminder(actor=actor, event_id=event.pk, minutes_before=minutes_before))

    @service_method(read_only=False, since="0.6.0")
    def remove_reminder(self, *, actor: Any, reminder_id: UUID, expected_revision: int) -> None:
        """Remove a reminder, with CAS.

        Autorização: update no calendário do evento.
        Erros: NotFound para lembrete inexistente ou de evento ilegível (a mesma resposta); Forbidden;
        Conflict("stale_revision").
        Efeitos: exclui logicamente o lembrete.
        """
        from . import reminders
        from .models import EventReminder

        reminder = EventReminder.objects.filter(pk=_identifier(reminder_id, "reminder_id")).first()
        if reminder is None:
            raise _not_found()
        _writable_event(actor, reminder.event_id)
        reminders.remove_reminder(actor=actor, reminder_id=reminder.pk, expected_revision=expected_revision)

    # ----------------------------------------------------------- managed events (#537)

    @service_method(read_only=False, since="0.6.0", allow_system=True)
    def put_managed_event(
        self,
        *,
        actor: Any,
        source_app: str,
        source_key: str,
        calendar_id: UUID,
        data: dict[str, Any],
        attendee_user_ids: tuple[UUID, ...] = (),
        source_path: str = "",
    ) -> EventInfo:
        """Create or update the event that projects a record of ``source_app`` (idempotent, silent).

        Only the app that owns the record passes its own label (the API is in process: checked by
        code review). The title names the kind of appointment, never a third party; the details
        stay in the source app, reached by ``source_path``.
        Autorização: update no calendário (``can_write``); em ``system_context`` da organização
        (``actor=None``), aceito, e o organizador é o dono do calendário.
        Erros: NotFound para calendário inexistente, ilegível ou de outra organização; Forbidden se
        for só legível; InvalidInput para ``description``, campo desconhecido, app de origem
        desconhecida, inativa ou a própria agenda, chave inválida, caminho fora de
        ``apps/<source_app>/``, horários inválidos ou participante que não é membro ativo.
        Efeitos: cria ou atualiza o único evento vivo da chave (privado por padrão, sem
        recorrência) e deixa os participantes exatamente em ``attendee_user_ids``, todos
        ``accepted``, sem notificar ninguém; repetir com os mesmos dados não muda revisão nem
        ``sequence``; outro ``calendar_id`` exclui o evento antigo e cria outro.
        """
        from skeleton.service_api import InvalidInput, as_uuid

        from . import services

        _require_source(source_app, source_key)
        if not isinstance(source_path, str):
            raise InvalidInput({"source_path": [gettext("Use um caminho relativo.")]})
        calendar = _managed_calendar(actor, calendar_id)
        if not isinstance(attendee_user_ids, tuple) or len(attendee_user_ids) > MAX_INVITES:
            raise InvalidInput(
                {"attendee_user_ids": [gettext("Informe até %(value1)s pessoas.") % {"value1": MAX_INVITES}]}
            )
        identifiers = tuple(as_uuid(value, field="attendee_user_ids") for value in attendee_user_ids)
        event = services.put_managed_event(
            actor=actor,
            calendar=calendar,
            source_app=source_app,
            source_key=source_key,
            source_path=source_path,
            data=dict(data) if isinstance(data, dict) else data,
            attendee_user_ids=identifiers,
        )
        return event_info(actor, event)

    @service_method(read_only=False, since="0.6.0", allow_system=True)
    def remove_managed_event(self, *, actor: Any, source_app: str, source_key: str) -> None:
        """Delete the event of ``(source_app, source_key)`` with its attendees; idempotent and silent.

        Autorização: update no calendário do evento; em ``system_context`` da organização, aceito.
        Erros: Forbidden se o evento for só legível; InvalidInput para app de origem desconhecida,
        inativa ou a própria agenda, ou chave inválida. Sem evento (ou ilegível): nada muda.
        Efeitos: exclusão lógica do evento e dos participantes, sem notificação.
        """
        from skeleton.service_api import Forbidden

        from . import services
        from .policies import can_write

        _require_source(source_app, source_key)
        with transaction.atomic(using=current_context().using):
            event = services.managed_event_row(source_app, source_key, lock=True)
            if event is None:
                return
            if actor is not None:
                if not _readable_ids(actor, event):
                    return
                if not can_write(actor, event.calendar):
                    raise Forbidden()
            services.remove_event_rows(event)

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def managed_event(self, *, actor: Any, source_app: str, source_key: str) -> EventInfo | None:
        """The live event of ``(source_app, source_key)`` as the actor may see it, or ``None``.

        Autorização: leitura do calendário ou convite; em ``system_context`` da organização, aceito.
        Erros: InvalidInput para app de origem desconhecida, inativa ou a própria agenda, ou chave
        inválida.
        Efeitos: nenhum.
        """
        from . import services

        _require_source(source_app, source_key)
        event = services.managed_event_row(source_app, source_key)
        if event is None or (actor is not None and not _readable_ids(actor, event)):
            return None
        return event_info(actor, event)

    @service_method(read_only=True, since="0.5.0")
    def free_busy(
        self,
        *,
        actor: Any,
        user_ids: tuple[UUID, ...],
        start: datetime,
        end: datetime,
        include_attending: bool = False,
    ) -> tuple[BusyBlock, ...]:
        """Busy intervals of members in their personal calendars: never titles or details.

        With ``include_attending`` (#538), the events the person attends with a response other
        than ``declined``, in any calendar of the organization, count as busy too.
        Autorização: TablePermission read em skeleton_calendar.calendar; só pessoas com vínculo
        ativo nesta organização entram no resultado.
        Erros: Forbidden sem a permissão de tabela; InvalidInput para janela ou mais de 50 pessoas.
        Efeitos: nenhum.
        """
        from django.db.models import Q

        from skeleton.service_api import Forbidden, InvalidInput, as_uuid
        from skeleton.service_api.organizations import OrganizationService

        from .models import Event, EventAttendee
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
        # One query for everyone (#291), plus one for the invitations (#538).
        condition = Q(calendar__owner_id__in=members, calendar__kind=Calendar.Kind.PERSONAL)
        attending: dict[Any, set[Any]] = {}
        if include_attending:
            rows = (
                EventAttendee.objects.filter(
                    membership_id__in=[ref.membership_id for ref in refs],
                    membership__is_active=True,
                    event__deleted_at__isnull=True,
                )
                .exclude(response=EventAttendee.Response.DECLINED)
                .values_list("event_id", "membership__user_id")
            )
            for event_id, user_id in rows:
                attending.setdefault(event_id, set()).add(user_id)
            condition |= Q(pk__in=list(attending))
        events = Event.objects.filter(condition, status__in=("confirmed", "tentative"))
        blocks: list[BusyBlock] = []
        for master, occurrence in _expanded(events, start, end):
            if occurrence.event.status == "cancelled":
                continue
            # One block per person and occurrence: the owner who also attends counts once.
            people = set(attending.get(master.pk, ()))
            if master.calendar.kind == Calendar.Kind.PERSONAL and master.calendar.owner_id in members:
                people.add(master.calendar.owner_id)
            blocks.extend(BusyBlock(user_id, occurrence.starts_at, occurrence.ends_at) for user_id in people)
        blocks.sort(key=lambda block: (str(block.user_id), block.starts_at, block.ends_at))
        return tuple(blocks)

    @service_method(read_only=True, since="0.6.0")
    def calendar_busy(
        self, *, actor: Any, calendar_ids: tuple[UUID, ...], start: datetime, end: datetime
    ) -> tuple[CalendarBusyBlock, ...]:
        """Busy intervals of calendars the actor reads (rooms, equipment): only times (#538).

        Autorização: leitura de cada calendário.
        Erros: NotFound para calendário ilegível ou de outra organização; InvalidInput para janela
        inválida ou acima de 400 dias, mais de 50 calendários ou ocorrências demais.
        Efeitos: nenhum.
        """
        from skeleton.service_api import InvalidInput, NotFound, as_uuid

        from .models import Event
        from .policies import readable_calendars

        start, end = _window(start, end)
        if not isinstance(calendar_ids, tuple) or not 1 <= len(calendar_ids) <= 50:
            raise InvalidInput({"calendar_ids": [gettext("Informe de 1 a 50 calendários.")]})
        wanted = {as_uuid(value, field="calendar_ids") for value in calendar_ids}
        readable = set(readable_calendars(actor).filter(pk__in=wanted).values_list("pk", flat=True))
        if readable != wanted:
            raise NotFound()
        events = Event.objects.filter(calendar_id__in=readable, status__in=("confirmed", "tentative"))
        blocks = {
            CalendarBusyBlock(master.calendar_id, occurrence.starts_at, occurrence.ends_at)
            for master, occurrence in _expanded(events, start, end)
            if occurrence.event.status != "cancelled"
        }
        return tuple(sorted(blocks, key=lambda block: (str(block.calendar_id), block.starts_at, block.ends_at)))


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
    # Managed events (#537): the owning app and the path of its record, empty for a busy block.
    source_app: str = ""
    source_path: str = ""


def event_info(actor: Any, event: Any) -> EventInfo:
    from .policies import BUSY_TITLE, sees_details

    # ``actor=None`` is the owning app in a system context (#537): it wrote the content.
    details = actor is None or sees_details(actor, event)
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
        source_app=event.source_app if details else "",
        source_path=event.source_path if details else "",
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
    start_date: date | None = None  # All-day only: the first day (#312).
    end_date: date | None = None  # All-day only: the day after the last one (exclusive).
    source_app: str = ""  # Managed events (#537); empty for a busy block.
    source_path: str = ""


@dataclass(frozen=True, slots=True)
class AttendeeInfo:
    """One invitation: who, the RSVP and whether it is optional (#344)."""

    sysid: UUID
    event_id: UUID
    user_id: UUID
    response: str
    optional: bool
    revision: int


@dataclass(frozen=True, slots=True)
class ReminderInfo:
    """One reminder of an event (#344)."""

    sysid: UUID
    event_id: UUID
    minutes_before: int
    channel_hint: str
    revision: int


def attendee_info(row: Any) -> AttendeeInfo:
    return AttendeeInfo(row.pk, row.event_id, row.membership.user_id, row.response, row.optional, row.revision)


def reminder_info(row: Any) -> ReminderInfo:
    return ReminderInfo(row.pk, row.event_id, row.minutes_before, row.channel_hint, row.revision)


MAX_LIST = 500
MAX_INVITES = 100


def _idempotent_event(uid: str) -> Any:
    from .models import Event

    return Event.objects.filter(uid=uid, series__isnull=True).select_related("calendar").first()


def _not_found() -> Any:
    from skeleton.service_api import NotFound

    return NotFound()


def _identifier(value: Any, field: str) -> UUID:
    from skeleton.service_api import as_uuid

    return as_uuid(value, field=field)


def _aware(value: Any) -> datetime:
    from skeleton.service_api import InvalidInput

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidInput({"recurrence_id": [gettext("Use data e hora com fuso.")]})
    return value


def _writable_event(actor: Any, event_id: Any) -> Any:
    """The event the actor reads (NotFound otherwise) and writes (Forbidden otherwise)."""
    from skeleton.service_api import Forbidden

    from .policies import can_write

    event = _readable_event(actor, event_id)
    if not can_write(actor, event.calendar):
        raise Forbidden()
    return event


@dataclass(frozen=True, slots=True)
class BusyBlock:
    user_id: UUID
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class CalendarBusyBlock:
    """A busy interval of a calendar (#538): never a title or a detail."""

    calendar_id: UUID
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


def _expanded(events: Any, start: datetime, end: datetime) -> list[Any]:
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
    overrides: dict[Any, list[Any]] = {}
    recurring = [master.pk for master in masters if master.rrule]
    if recurring:
        for row in Event.objects.filter(series_id__in=recurring).select_related("calendar"):
            overrides.setdefault(row.series_id, []).append(row)
    pairs: list[Any] = []
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
                start_date=occurrence.starts_at.date() if target.all_day else None,
                end_date=occurrence.ends_at.date() if target.all_day else None,
                source_app=event.source_app if details else "",
                source_path=event.source_path if details else "",
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


def _require_source(source_app: Any, source_key: Any) -> None:
    """A registered, active app other than the calendar, and an opaque key (#537)."""
    import re

    from skeleton.service_api import InvalidInput
    from skeleton.system.applications import application_state, require_application_enabled

    if (
        not isinstance(source_app, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", source_app)
        or source_app == APP_LABEL
    ):
        raise InvalidInput({"source_app": [gettext("Informe o label de outra app.")]})
    try:
        application_state(source_app)
        require_application_enabled(source_app)
    except (KeyError, PermissionDenied):
        raise InvalidInput({"source_app": [gettext("A app de origem não está ativa nesta organização.")]}) from None
    from .services import MANAGED_KEY

    if not isinstance(source_key, str) or not re.fullmatch(MANAGED_KEY, source_key):
        raise InvalidInput({"source_key": [gettext("Use de 1 a 200 letras, dígitos e : . _ -.")]})


def _managed_calendar(actor: Any, calendar_id: Any) -> Calendar:
    """The calendar a managed event goes to: writable by the actor, or any of the tenant for the system."""
    from skeleton.service_api import Forbidden, NotFound, as_uuid

    from .policies import can_write

    if actor is None:
        calendar = Calendar.objects.filter(pk=as_uuid(calendar_id, field="calendar_id")).first()
        if calendar is None:
            raise NotFound()
        return calendar
    calendar = _readable_calendar(actor, calendar_id)
    if not can_write(actor, calendar):
        raise Forbidden()
    return calendar


def _readable_ids(actor: Any, event: Any) -> bool:
    from .policies import readable_events

    return readable_events(actor).filter(pk=event.pk).exists()
