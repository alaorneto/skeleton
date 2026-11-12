"""AuditService: the authorized history of one record, with sensitive values redacted (#106)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext

from .base import ServiceAPI, service_method
from .dto import DEFAULT_PAGE_SIZE, Page, as_utc, as_uuid, paginate, validate_page_size
from .errors import InvalidInput, NotFound
from .identity import _user

Scalar = str | int | bool | None


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One audited column; a redacted value is the literal ``[REDACTED]``."""

    name: str
    old: Scalar
    new: Scalar
    redacted: bool


@dataclass(frozen=True, slots=True)
class AuditEntry:
    sysid: UUID
    occurred_at: datetime
    operation: str
    record_revision: int
    actor_id: UUID | None
    request_id: UUID | None
    source: str
    changes: tuple[FieldChange, ...]


def _scalar(value: object) -> Scalar:
    if value is None or isinstance(value, bool | int | str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _entry(event: Any) -> AuditEntry:
    from skeleton.audit.presentation import visible_audit_changes

    changes = tuple(
        FieldChange(name, _scalar(change.get("old")), _scalar(change.get("new")), bool(change.get("redacted")))
        for name, change in sorted(visible_audit_changes(event).items())
    )
    return AuditEntry(
        sysid=event.pk,
        occurred_at=as_utc(event.created_at),
        operation=event.operation,
        record_revision=event.record_revision,
        actor_id=event.actor_sysid,
        request_id=event.request_id,
        source=event.source,
        changes=changes,
    )


def _record_visible(model: Any, actor: Any, record_id: UUID) -> bool:
    from skeleton.access.policies import authorized_queryset, has_table_permission

    try:
        if authorized_queryset(model, user=actor, action="read").filter(pk=record_id).exists():
            return True
        if has_table_permission(model, user=actor, action="restore"):
            return (
                authorized_queryset(model, user=actor, action="restore", include_deleted=True)
                .filter(pk=record_id, deleted_at__isnull=False)
                .exists()
            )
    except PermissionDenied:
        return False
    return False


class AuditService(ServiceAPI):
    """Same rules as ``authorized_audit_queryset``: filtered before paginating."""

    @service_method(read_only=True, since="0.5.0")
    def history(
        self,
        *,
        actor: AbstractBaseUser,
        table_key: str,
        record_id: UUID,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[AuditEntry]:
        """Return the history of one record, most recent first.

        Autorização: administrador da organização, ou TablePermission ``audit`` mais leitura do
        registro atual (se excluído, restauração autorizada). Só eventos da organização atual.
        Erros: NotFound sem essas permissões, para tabela desconhecida e para registro de outra
        organização; InvalidInput para identificador, cursor ou tamanho de página inválidos.
        Efeitos: nenhum.
        """
        from skeleton.access.policies import authorized_audit_queryset, has_table_permission, is_organization_admin
        from skeleton.common.models import SecuredRecord
        from skeleton.system.registry import get_spec

        if not isinstance(table_key, str):
            raise InvalidInput({"table_key": [gettext("Use o identificador da tabela.")]})
        identifier = as_uuid(record_id, field="record_id")
        try:
            spec = get_spec(table_key)
        except KeyError:
            raise NotFound() from None
        if not spec.expose_api or not issubclass(spec.model, SecuredRecord):
            raise NotFound()
        if not is_organization_admin(_user(actor)):
            try:
                allowed = has_table_permission(spec.model, user=_user(actor), action="audit")
            except PermissionDenied:
                allowed = False
            if not allowed or not _record_visible(spec.model, _user(actor), identifier):
                raise NotFound()
        events = (
            authorized_audit_queryset(_user(actor))
            .filter(model_label=spec.model._meta.label_lower, record_sysid=identifier)
            .order_by("-created_at", "-record_revision", "sysid")
        )
        return paginate(events, convert=_entry, page_size=page_size, cursor=cursor)


@dataclass(frozen=True, slots=True)
class AuditLogEvent:
    """One event of the organization audit log, as the audit page and ``/audit/`` show it.

    ``changes_json`` is the JSON object of the visible columns (``visible_audit_changes``):
    values keep their JSON types and redacted columns keep the marker written by the trigger.
    """

    sysid: UUID
    created_at: datetime
    organization_id: UUID | None
    table_name: str
    model_label: str
    record_sysid: UUID
    record_revision: int
    operation: str
    actor_sysid: UUID | None
    request_id: UUID | None
    source: str
    db_principal: str
    changes_json: str


@dataclass(frozen=True, slots=True)
class AuditLogPage:
    """A numbered page of the audit log: ``count`` is the total of authorized events."""

    items: tuple[AuditLogEvent, ...]
    count: int
    page: int
    page_size: int


def _log_event(event: Any) -> AuditLogEvent:
    from skeleton.audit.presentation import visible_audit_changes

    return AuditLogEvent(
        sysid=event.pk,
        created_at=as_utc(event.created_at),
        organization_id=event.organization_id,
        table_name=event.table_name,
        model_label=event.model_label,
        record_sysid=event.record_sysid,
        record_revision=event.record_revision,
        operation=event.operation,
        actor_sysid=event.actor_sysid,
        request_id=event.request_id,
        source=event.source,
        db_principal=event.db_principal,
        changes_json=json.dumps(visible_audit_changes(event)),
    )


@dataclass(frozen=True, slots=True)
class DomainEventSummary:
    """A domain event correlated with an audited request; identifiers only, never ``data``."""

    event_type: str
    table_key: str
    record_id: UUID
    record_revision: int | None
    occurred_at: datetime


def _authorized_log(actor: AbstractBaseUser) -> Any:
    from skeleton.access.policies import authorized_audit_queryset

    return authorized_audit_queryset(_user(actor)).order_by("-created_at", "sysid")


class AuditLogService(ServiceAPI):
    """The audit log of the core surfaces, filtered before counting.

    In an organization (``/o/<org>/audit/``) it lists that organization's events; without one
    (``/platform/audit/``) it lists the global events, for platform administrators only.
    """

    @service_method(read_only=True, since="0.5.0", tenant=False)
    def events(
        self,
        *,
        actor: AbstractBaseUser,
        model_label: str | None = None,
        record_id: UUID | str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        actor_id: UUID | str | None = None,
        operation: str | None = None,
        request_id: UUID | str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> AuditLogPage:
        """Return one numbered page of the events the actor may read, most recent first.

        ``since`` (inclusive) and ``until`` (exclusive) bound ``created_at``; ``actor_id``,
        ``operation`` and ``request_id`` narrow further (#184). Filters only narrow what the
        authorization already allows.

        Autorização: as regras de ``authorized_audit_queryset``: administrador da organização vê
        todos os eventos dela; os demais, os eventos dos registros que leem (ou restauram) nas
        tabelas em que têm TablePermission ``audit``. Só eventos da organização atual; sem
        organização no contexto, os eventos globais, só para o administrador da plataforma.
        Erros: InvalidInput para filtro, página ou tamanho inválidos; Forbidden sem vínculo ativo
        ou, sem organização, para quem não administra a plataforma.
        Efeitos: nenhum.
        """
        if model_label is not None and not isinstance(model_label, str):
            raise InvalidInput({"model_label": [gettext("Use o rótulo do modelo.")]})
        size = validate_page_size(page_size)
        if type(page) is not int or page < 1:
            raise InvalidInput({"page": [gettext("Use um inteiro maior que zero.")]})
        events = _authorized_log(actor)
        if model_label is not None:
            events = events.filter(model_label=model_label)
        if record_id is not None:
            events = events.filter(record_sysid=as_uuid(record_id, field="record_id"))
        for name, value in (("since", since), ("until", until)):
            if value is not None and not isinstance(value, datetime):
                raise InvalidInput({name: [gettext("Use data e hora.")]})
        if since is not None:
            events = events.filter(created_at__gte=as_utc(since))
        if until is not None:
            events = events.filter(created_at__lt=as_utc(until))
        if actor_id is not None:
            events = events.filter(actor_sysid=as_uuid(actor_id, field="actor_id"))
        if operation is not None:
            from skeleton.audit.models import AuditEvent

            if operation not in AuditEvent.Operation.values:
                raise InvalidInput({"operation": [gettext("Operação desconhecida.")]})
            events = events.filter(operation=operation)
        if request_id is not None:
            events = events.filter(request_id=as_uuid(request_id, field="request_id"))
        offset = (page - 1) * size
        return AuditLogPage(
            items=tuple(_log_event(event) for event in events[offset : offset + size]),
            count=events.count(),
            page=page,
            page_size=size,
        )

    @service_method(read_only=True, since="0.5.0", tenant=False)
    def event(self, *, actor: AbstractBaseUser, event_id: UUID | str) -> AuditLogEvent:
        """Return one event of the log.

        Autorização: as mesmas de ``events``.
        Erros: NotFound para evento inexistente, de outra organização ou não autorizado;
        InvalidInput para identificador inválido.
        Efeitos: nenhum.
        """
        event = _authorized_log(actor).filter(pk=as_uuid(event_id, field="event_id")).first()
        if event is None:
            raise NotFound()
        return _log_event(event)

    @service_method(read_only=True, since="0.5.0")
    def related_domain_events(
        self, *, actor: AbstractBaseUser, request_id: UUID | str
    ) -> tuple[DomainEventSummary, ...]:
        """Return the domain events still retained that the same request published (#184).

        Autorização: administrador da organização (os eventos trazem identificadores de qualquer
        tabela); os demais recebem Forbidden. Só eventos da organização atual.
        Erros: InvalidInput para identificador inválido; Forbidden para quem não administra.
        Efeitos: nenhum.
        """
        from skeleton.access.policies import is_organization_admin
        from skeleton.events.models import DomainEvent

        from .errors import Forbidden

        if not is_organization_admin(_user(actor)):
            raise Forbidden()
        rows = DomainEvent.objects.filter(request_id=as_uuid(request_id, field="request_id")).order_by(
            "occurred_at", "sysid"
        )[:100]
        return tuple(
            DomainEventSummary(
                event_type=row.event_type,
                table_key=row.table_key,
                record_id=row.record_sysid,
                record_revision=row.record_revision,
                occurred_at=as_utc(row.occurred_at),
            )
            for row in rows
        )
