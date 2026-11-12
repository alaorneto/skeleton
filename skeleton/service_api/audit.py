"""AuditService: the authorized history of one record, with sensitive values redacted (#106)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied

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


def history_allowed(spec: Any, actor: Any, record_id: UUID) -> bool:
    """Whether the actor reads the audit history of one record (AuditService and the timeline, #185).

    Organization administrators always do; anyone else needs TablePermission ``audit`` and to
    read the record now (or, if it is deleted, to restore it).
    """
    from skeleton.access.policies import has_table_permission, is_organization_admin

    user = _user(actor)
    if is_organization_admin(user):
        return True
    try:
        allowed = has_table_permission(spec.model, user=user, action="audit")
    except PermissionDenied:
        return False
    return allowed and _record_visible(spec.model, user, record_id)


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
        from skeleton.access.policies import authorized_audit_queryset
        from skeleton.common.models import SecuredRecord
        from skeleton.system.registry import get_spec

        if not isinstance(table_key, str):
            raise InvalidInput({"table_key": ["Use o identificador da tabela."]})
        identifier = as_uuid(record_id, field="record_id")
        try:
            spec = get_spec(table_key)
        except KeyError:
            raise NotFound() from None
        if not spec.expose_api or not issubclass(spec.model, SecuredRecord):
            raise NotFound()
        if not history_allowed(spec, actor, identifier):
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


def _authorized_log(actor: AbstractBaseUser) -> Any:
    from skeleton.access.policies import authorized_audit_queryset

    return authorized_audit_queryset(_user(actor)).order_by("-created_at", "sysid")


AUDIT_OPERATIONS = frozenset({"create", "update", "soft_delete", "restore", "hard_delete"})


def _moment(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidInput({field: ["Use data e hora com fuso."]})
    return value


def _filtered_log(
    actor: AbstractBaseUser,
    *,
    model_label: object = None,
    record_id: object = None,
    occurred_from: object = None,
    occurred_to: object = None,
    actor_id: object = None,
    operation: object = None,
    request_id: object = None,
) -> Any:
    """The authorized log narrowed by the allowlisted filters; validation precedes the query."""
    if model_label is not None and (not isinstance(model_label, str) or len(model_label) > 255):
        raise InvalidInput({"model_label": ["Use o rótulo do modelo."]})
    if operation is not None and operation not in AUDIT_OPERATIONS:
        raise InvalidInput({"operation": ["Operação desconhecida."]})
    lookups: dict[str, object] = {}
    if model_label is not None:
        lookups["model_label"] = model_label
    if record_id is not None:
        lookups["record_sysid"] = as_uuid(record_id, field="record_id")
    if occurred_from is not None:
        lookups["created_at__gte"] = _moment(occurred_from, "occurred_from")
    if occurred_to is not None:
        lookups["created_at__lte"] = _moment(occurred_to, "occurred_to")
    if occurred_from is not None and occurred_to is not None and occurred_from > occurred_to:  # type: ignore[operator]
        raise InvalidInput({"occurred_from": ["O início é posterior ao fim."]})
    if actor_id is not None:
        lookups["actor_sysid"] = as_uuid(actor_id, field="actor_id")
    if operation is not None:
        lookups["operation"] = operation
    if request_id is not None:
        lookups["request_id"] = as_uuid(request_id, field="request_id")
    return _authorized_log(actor).filter(**lookups)


@dataclass(frozen=True, slots=True)
class CorrelatedDomainEvent:
    """A retained domain event of the same request as visible audit events (#184).

    Only identifiers and the type: the event payload stays with the event consumers.
    """

    sysid: UUID
    occurred_at: datetime
    event_type: str
    version: int
    table_key: str
    record_sysid: UUID
    record_revision: int | None
    actor_id: UUID | None
    source: str


#: Bound of audit events of one request read to correlate domain events.
CORRELATION_LIMIT = 500


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
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        actor_id: UUID | str | None = None,
        operation: str | None = None,
        request_id: UUID | str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> AuditLogPage:
        """Return one numbered page of the events the actor may read, most recent first.

        Os filtros (#184) restringem o conjunto já autorizado: período inclusivo
        (``occurred_from``/``occurred_to``, com fuso), ator, operação, ``request_id``, tabela e
        registro. Nenhum filtro amplia a visibilidade.
        Autorização: as regras de ``authorized_audit_queryset``: administrador da organização vê
        todos os eventos dela; os demais, os eventos dos registros que leem (ou restauram) nas
        tabelas em que têm TablePermission ``audit``. Só eventos da organização atual; sem
        organização no contexto, os eventos globais, só para o administrador da plataforma.
        Erros: InvalidInput para filtro, página ou tamanho inválidos (inclusive período invertido
        ou data sem fuso); Forbidden sem vínculo ativo ou, sem organização, para quem não
        administra a plataforma.
        Efeitos: nenhum.
        """
        events = _filtered_log(
            actor,
            model_label=model_label,
            record_id=record_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            actor_id=actor_id,
            operation=operation,
            request_id=request_id,
        )
        size = validate_page_size(page_size)
        if type(page) is not int or page < 1:
            raise InvalidInput({"page": ["Use um inteiro maior que zero."]})
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

    @service_method(read_only=True, since="0.6.0", tenant=False)
    def domain_events(self, *, actor: AbstractBaseUser, request_id: UUID | str) -> tuple[CorrelatedDomainEvent, ...]:
        """Return the retained domain events of one request, oldest first (#184).

        Só aparecem eventos de domínio de registros cujos eventos de auditoria, do mesmo
        ``request_id``, o ator lê: os filtros nunca revelam o que a auditoria esconderia.
        Eventos já apagados pela retenção do outbox não aparecem.
        Autorização: as mesmas de ``events``; sem organização no contexto não há eventos de
        domínio (eles sempre pertencem a uma organização).
        Erros: InvalidInput para identificador inválido; Forbidden como em ``events``.
        Efeitos: nenhum.
        """
        from skeleton.events.models import DomainEvent
        from skeleton.tenancy.context import current_context

        identifier = as_uuid(request_id, field="request_id")
        visible = list(
            _authorized_log(actor)
            .filter(request_id=identifier)
            .values_list("model_label", "record_sysid")[:CORRELATION_LIMIT]
        )
        if current_context().organization_id is None or not visible:
            return ()
        pairs = {(label, record) for label, record in visible}
        rows = DomainEvent.objects.filter(
            request_id=identifier, record_sysid__in={record for _, record in pairs}
        ).order_by("occurred_at", "sysid")[:CORRELATION_LIMIT]
        return tuple(
            CorrelatedDomainEvent(
                sysid=row.pk,
                occurred_at=as_utc(row.occurred_at),
                event_type=row.event_type,
                version=row.version,
                table_key=row.table_key,
                record_sysid=row.record_sysid,
                record_revision=row.record_revision,
                actor_id=row.actor_id,
                source=row.source,
            )
            for row in rows
            if (row.table_key, row.record_sysid) in pairs
        )
