"""TransferService: request CSV exports of authorized records and follow them (#182)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser

from .base import ServiceAPI, service_method
from .dto import DEFAULT_PAGE_SIZE, Page, as_utc, as_uuid, paginate
from .errors import InvalidInput, NotFound


@dataclass(frozen=True, slots=True)
class TransferInfo:
    """A transfer of the actor; the storage key and the filters never leave the core."""

    sysid: UUID
    kind: str
    table_key: str
    state: str
    row_count: int | None
    size: int | None
    filename: str
    error_code: str
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime


def _info(transfer: Any) -> TransferInfo:
    return TransferInfo(
        sysid=transfer.pk,
        kind=transfer.kind,
        table_key=transfer.table_key,
        state=transfer.state,
        row_count=transfer.row_count,
        size=transfer.size,
        filename=transfer.filename,
        error_code=transfer.error_code,
        created_at=as_utc(transfer.created_at),
        finished_at=as_utc(transfer.finished_at) if transfer.finished_at else None,
        expires_at=as_utc(transfer.expires_at),
    )


def _spec(table_key: object) -> Any:
    from django.http import Http404

    from skeleton.web.services import exposed_spec

    if not isinstance(table_key, str):
        raise InvalidInput({"table_key": ["Use o identificador da tabela."]})
    try:
        return exposed_spec(table_key)
    except Http404:
        raise NotFound() from None


class TransferService(ServiceAPI):
    """Exports run in jobs as the requester; only the requester sees and downloads them."""

    @service_method(read_only=False, since="0.6.0")
    def request_export(
        self,
        *,
        actor: AbstractBaseUser,
        table_key: str,
        filters: tuple[tuple[str, str], ...] = (),
        idempotency_key: str | None = None,
    ) -> TransferInfo:
        """Request a CSV of the records the actor reads, with the exact filters of the list.

        Autorização: TablePermission ``read`` na tabela exposta; o job relê os registros como o
        ator, com as mesmas regras por registro, e só exporta ``read_fields`` não sensíveis.
        Erros: NotFound para tabela desconhecida ou não exposta; Forbidden sem leitura;
        InvalidInput para filtro não permitido ou inválido, ou chave de idempotência reutilizada
        em outro pedido.
        Efeitos: grava o pedido e um job ``transfer.export`` na transação do chamador; o
        rollback desfaz ambos. Ao fim, o job notifica o ator (``transfer.finished``).
        """
        from skeleton.transfer.exports import request_export

        if not isinstance(filters, tuple) or any(not isinstance(item, tuple) or len(item) != 2 for item in filters):
            raise InvalidInput({"filters": ["Use pares (campo, valor)."]})
        return _info(
            request_export(_spec(table_key), user=actor, filters=dict(filters), idempotency_key=idempotency_key)
        )

    @service_method(read_only=True, since="0.6.0")
    def transfer(self, *, actor: AbstractBaseUser, transfer_id: UUID) -> TransferInfo:
        """One transfer of the actor in the current organization.

        Autorização: só quem pediu; nem o administrador da organização vê pedidos de outra pessoa.
        Erros: NotFound para transferência inexistente, de outra pessoa ou de outra organização;
        InvalidInput para identificador inválido.
        Efeitos: nenhum.
        """
        from skeleton.transfer.exports import transfers_of

        row = transfers_of(actor).filter(pk=as_uuid(transfer_id, field="transfer_id")).first()
        if row is None:
            raise NotFound()
        return _info(row)

    @service_method(read_only=True, since="0.6.0")
    def transfers(
        self, *, actor: AbstractBaseUser, cursor: str | None = None, page_size: int = DEFAULT_PAGE_SIZE
    ) -> Page[TransferInfo]:
        """The actor's transfers in the current organization, newest first, until retention removes them.

        Autorização: só os pedidos do próprio ator.
        Erros: InvalidInput para cursor ou tamanho de página inválidos.
        Efeitos: nenhum.
        """
        from skeleton.transfer.exports import transfers_of

        return paginate(transfers_of(actor), convert=_info, page_size=page_size, cursor=cursor)
