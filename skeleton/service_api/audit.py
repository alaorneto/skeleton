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
from .dto import DEFAULT_PAGE_SIZE, Page, as_utc, as_uuid, paginate
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
            raise InvalidInput({"table_key": ["Use o identificador da tabela."]})
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
