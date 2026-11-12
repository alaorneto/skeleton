"""NotificationService: the only door for apps to notify and to read the actor's inbox (#107)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext

from skeleton.notifications.services import Recipients

from .applications import calling_app_label
from .base import ServiceAPI, service_method
from .dto import DEFAULT_PAGE_SIZE, Page, as_utc, as_uuid, paginate
from .errors import Forbidden, InvalidInput, NotFound

__all__ = ["NotificationItem", "NotificationReceipt", "NotificationService", "NotificationValue", "Recipients"]

Scalar = str | int | bool | None


@dataclass(frozen=True, slots=True)
class NotificationReceipt:
    sysid: UUID
    type_code: str
    dedupe_key: str | None


@dataclass(frozen=True, slots=True)
class NotificationValue:
    name: str
    value: Scalar


@dataclass(frozen=True, slots=True)
class NotificationItem:
    sysid: UUID
    organization_id: UUID
    type_code: str
    label: str
    snapshot: tuple[NotificationValue, ...]
    payload: tuple[NotificationValue, ...]
    count: int
    read: bool
    archived: bool
    created_at: datetime
    updated_at: datetime


def _values(mapping: dict[str, Any]) -> tuple[NotificationValue, ...]:
    return tuple(NotificationValue(str(name), value) for name, value in sorted(mapping.items()))


def _item(row: Any) -> NotificationItem:
    from skeleton.notifications.inbox import row_data

    data = row_data(row)
    return NotificationItem(
        sysid=data["sysid"],
        organization_id=data["organization_id"],
        type_code=data["type_code"],
        label=data["label"],
        snapshot=_values(data["snapshot"]),
        payload=_values(data["payload"]),
        count=data["count"],
        read=data["read"],
        archived=data["archived"],
        created_at=as_utc(data["created_at"]),
        updated_at=as_utc(data["updated_at"]),
    )


def _ids(ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
    if not isinstance(ids, tuple | list) or not 1 <= len(ids) <= 100:
        raise InvalidInput({"ids": [gettext("Informe de 1 a 100 identificadores.")]})
    return tuple(as_uuid(value, field="ids") for value in ids)


class NotificationService(ServiceAPI):
    """Types stay registered by each app; only the owning app may notify with its type."""

    @service_method(read_only=False, since="0.5.0", allow_system=True)
    def notify(
        self,
        *,
        actor: AbstractBaseUser | None,
        type_code: str,
        recipients: Recipients,
        payload: tuple[tuple[str, str | int | bool | None], ...] = (),
        target_table_key: str | None = None,
        target_id: UUID | None = None,
        dedupe_key: str | None = None,
    ) -> NotificationReceipt:
        """Record a notification in the caller's transaction and enqueue its fan-out.

        Autorização: a app chamadora precisa ser a dona do tipo (``owner_app``); o ator precisa
        ler o alvo; cada destinatário é reautorizado no alvo durante o fan-out. Em consumidores de
        eventos (``system_context``) ``actor=None``.
        Erros: Forbidden para tipo de outra app; NotFound para alvo inexistente, de outra
        organização ou não legível; InvalidInput para tipo, payload ou destinatários inválidos.
        Efeitos: grava a notificação e um job de fan-out; o rollback do chamador desfaz ambos.
        """
        from skeleton.notifications.registry import get_notification_type
        from skeleton.notifications.services import notify

        try:
            kind = get_notification_type(type_code)
        except KeyError:
            raise InvalidInput({"type_code": [gettext("Tipo de notificação desconhecido.")]}) from None
        if calling_app_label() != (kind.owner_app or None):
            raise Forbidden(gettext("A app só notifica com os próprios tipos."))
        target = None
        if target_id is not None or target_table_key is not None:
            if target_table_key != kind.target_table_key or target_id is None:
                raise InvalidInput({"target": [gettext("Informe o alvo da tabela declarada no tipo.")]})
            target = self._readable_target(actor, kind.target_table_key, as_uuid(target_id, field="target_id"))
        notification = notify(
            kind.code,
            recipients=recipients,
            target=target,
            payload=dict(payload),
            dedupe_key=dedupe_key,
        )
        return NotificationReceipt(notification.pk, notification.type_code, notification.dedupe_key)

    @staticmethod
    def _readable_target(actor: Any, table_key: str, identifier: UUID) -> Any:
        from skeleton.access.policies import authorized_queryset
        from skeleton.system.registry import get_spec

        model = get_spec(table_key).model
        try:
            if actor is None:
                record = model.objects.filter(pk=identifier).first()
            else:
                record = authorized_queryset(model, user=actor, action="read").filter(pk=identifier).first()
        except PermissionDenied:
            record = None
        if record is None:
            raise NotFound()
        return record

    @service_method(read_only=True, since="0.5.0", tenant=False)
    def unread_count(self, *, actor: AbstractBaseUser) -> int:
        """Unread, unarchived notifications of the actor in every organization with an active membership.

        Autorização: o próprio ator.
        Erros: nenhum além do contrato do contexto.
        Efeitos: nenhum.
        """
        from skeleton.notifications.inbox import unread_count

        return unread_count(actor)

    @service_method(read_only=True, since="0.5.0", tenant=False)
    def inbox(
        self,
        *,
        actor: AbstractBaseUser,
        cursor: str | None = None,
        include_archived: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[NotificationItem]:
        """A page of the actor's notifications: the current organization, or every active one without tenant.

        Autorização: só as linhas do próprio ator.
        Erros: InvalidInput para cursor ou tamanho de página inválidos.
        Efeitos: nenhum.
        """
        from skeleton.notifications.inbox import inbox_queryset

        if type(include_archived) is not bool:
            raise InvalidInput({"include_archived": [gettext("Use true ou false.")]})
        rows = inbox_queryset(actor, include_archived=include_archived)
        return paginate(rows, convert=_item, page_size=page_size, cursor=cursor)

    @service_method(read_only=False, since="0.5.0")
    def mark_read(self, *, actor: AbstractBaseUser, ids: tuple[UUID, ...], read: bool = True) -> int:
        """Mark the actor's rows as read (or unread with ``read=False``); idempotent.

        Autorização: só linhas do ator, no tenant atual.
        Erros: NotFound se algum id for de outra pessoa ou não existir; InvalidInput.
        Efeitos: atualiza as linhas (tabela operacional, sem auditoria por linha).
        """
        from skeleton.notifications.inbox import mark_read

        return mark_read(actor, _ids(ids), read=read)

    @service_method(read_only=False, since="0.5.0")
    def archive(self, *, actor: AbstractBaseUser, ids: tuple[UUID, ...]) -> int:
        """Archive the actor's rows; idempotent.

        Autorização: só linhas do ator, no tenant atual.
        Erros: NotFound se algum id for de outra pessoa ou não existir; InvalidInput.
        Efeitos: atualiza as linhas.
        """
        from skeleton.notifications.inbox import archive

        return archive(actor, _ids(ids))
