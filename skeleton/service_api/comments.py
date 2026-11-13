"""CommentService: comment records, authorized by the target, like FileService (#187)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.http import Http404

from .base import ServiceAPI, service_method
from .dto import DEFAULT_PAGE_SIZE, Page, as_utc, as_uuid, paginate
from .errors import Conflict, InvalidInput, NotFound


@dataclass(frozen=True, slots=True)
class CommentInfo:
    """A comment as plain text; render it escaped (``skeleton.comments.text.linkify``)."""

    sysid: UUID
    table_key: str
    record_id: UUID
    author_id: UUID | None
    body: str
    revision: int
    created_at: datetime
    edited_at: datetime | None
    deleted: bool


def _info(comment: Any) -> CommentInfo:
    return CommentInfo(
        sysid=comment.pk,
        table_key=comment.target_table.model_label,
        record_id=comment.target_sysid,
        author_id=comment.created_by,
        body=comment.body,
        revision=comment.revision,
        created_at=as_utc(comment.created_at),
        edited_at=as_utc(comment.edited_at) if comment.edited_at else None,
        deleted=comment.deleted_at is not None,
    )


def _spec(table_key: object) -> Any:
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import get_spec

    if not isinstance(table_key, str):
        raise InvalidInput({"table_key": ["Use o identificador da tabela."]})
    try:
        spec = get_spec(table_key)
    except KeyError:
        raise NotFound() from None
    if not spec.expose_api or not issubclass(spec.model, SecuredRecord) or spec.comments is None:
        raise NotFound()
    return spec


def _call(function: Any, *args: Any, **kwargs: Any) -> Any:
    from skeleton.common.exceptions import ConcurrentUpdate

    try:
        return function(*args, **kwargs)
    except Http404:
        raise NotFound() from None
    except ConcurrentUpdate:
        raise Conflict("stale_revision") from None


class CommentService(ServiceAPI):
    """Who reads a record reads its comments; only the author edits; author and admin delete."""

    @service_method(read_only=True, since="0.6.0")
    def comments(
        self,
        *,
        actor: AbstractBaseUser,
        table_key: str,
        record_id: UUID,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[CommentInfo]:
        """A page of the record's live comments, oldest first.

        Autorização: leitura do registro alvo, numa tabela com ``CommentPolicy``.
        Erros: NotFound para tabela sem a política e alvo inexistente, de outra organização,
        excluído ou ilegível; InvalidInput para cursor ou tamanho de página inválidos.
        Efeitos: nenhum.
        """
        from skeleton.comments import services

        rows = _call(services.comments_for, _spec(table_key), as_uuid(record_id, field="record_id"), user=actor)
        return paginate(rows.select_related("target_table"), convert=_info, page_size=page_size, cursor=cursor)

    @service_method(read_only=False, since="0.6.0")
    def add_comment(self, *, actor: AbstractBaseUser, table_key: str, record_id: UUID, body: str) -> CommentInfo:
        """Comment a record with plain text (up to 5,000 characters; no Markdown or HTML).

        Autorização: leitura do registro alvo, numa tabela com ``CommentPolicy``.
        Erros: NotFound como em ``comments``; InvalidInput para corpo vazio, longo demais, com
        caracteres de controle ou mencionando (``@[nome](sysid)``) quem não é membro ativo ou não
        lê o registro.
        Efeitos: grava o comentário (auditado) e uma notificação ``comments.mention`` por pessoa
        mencionada, na transação do chamador.
        """
        from skeleton.comments import services

        comment = _call(services.add, _spec(table_key), as_uuid(record_id, field="record_id"), user=actor, body=body)
        return _info(comment)

    @service_method(read_only=False, since="0.6.0")
    def edit_comment(
        self,
        *,
        actor: AbstractBaseUser,
        table_key: str,
        record_id: UUID,
        comment_id: UUID,
        body: str,
        expected_revision: int,
    ) -> CommentInfo:
        """Replace the body of the actor's own comment and mark it edited.

        Autorização: só o autor, que ainda lê o registro.
        Erros: NotFound para comentário inexistente, excluído ou de alvo ilegível; Forbidden para
        quem não é o autor; Conflict("stale_revision") para revisão desatualizada; InvalidInput.
        Efeitos: grava a nova versão (auditada); só quem é mencionado pela primeira vez neste
        comentário é notificado.
        """
        from skeleton.comments import services

        comment = _call(
            services.edit,
            _spec(table_key),
            as_uuid(record_id, field="record_id"),
            as_uuid(comment_id, field="comment_id"),
            user=actor,
            body=body,
            revision=expected_revision,
        )
        return _info(comment)

    @service_method(read_only=False, since="0.6.0")
    def delete_comment(
        self, *, actor: AbstractBaseUser, table_key: str, record_id: UUID, comment_id: UUID, expected_revision: int
    ) -> None:
        """Soft delete a comment: its author, or the organization administrator moderating.

        Autorização: autor ou administrador da organização, que leem o registro.
        Erros: NotFound; Forbidden para outra pessoa; Conflict("stale_revision").
        Efeitos: exclusão lógica auditada; ``restore_comment`` a desfaz.
        """
        from skeleton.comments import services

        _call(
            services.remove,
            _spec(table_key),
            as_uuid(record_id, field="record_id"),
            as_uuid(comment_id, field="comment_id"),
            user=actor,
            revision=expected_revision,
        )

    @service_method(read_only=False, since="0.6.0")
    def restore_comment(
        self, *, actor: AbstractBaseUser, table_key: str, record_id: UUID, comment_id: UUID, expected_revision: int
    ) -> CommentInfo:
        """Restore a deleted comment with its UUID.

        Autorização: autor ou administrador da organização, que leem o registro.
        Erros: NotFound para comentário vivo ou inexistente; Forbidden; Conflict("stale_revision").
        Efeitos: restauração auditada.
        """
        from skeleton.comments import services

        comment = _call(
            services.restore,
            _spec(table_key),
            as_uuid(record_id, field="record_id"),
            as_uuid(comment_id, field="comment_id"),
            user=actor,
            revision=expected_revision,
        )
        return _info(comment)
