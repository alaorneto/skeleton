"""FileService: attach, list, download and remove files of records, authorized by the target (#109)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser

from .base import ServiceAPI, service_method
from .dto import DEFAULT_PAGE_SIZE, Page, as_utc, as_uuid, paginate
from .errors import Conflict, InvalidInput, NotFound


@dataclass(frozen=True, slots=True)
class AttachmentInfo:
    """Public metadata; the storage key never leaves the core."""

    sysid: UUID
    table_key: str
    record_id: UUID
    filename: str
    content_type: str
    size: int
    sha256: str
    state: str
    revision: int
    created_at: datetime


def _spec(table_key: str) -> Any:
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import get_spec

    try:
        spec = get_spec(table_key)
    except (KeyError, TypeError):
        raise NotFound() from None
    if not spec.expose_api or not issubclass(spec.model, SecuredRecord) or spec.attachments is None:
        raise NotFound()
    return spec


def _info(attachment: Any) -> AttachmentInfo:
    return AttachmentInfo(
        sysid=attachment.pk,
        table_key=attachment.target_table.model_label,
        record_id=attachment.target_sysid,
        filename=attachment.filename,
        content_type=attachment.content_type,
        size=attachment.size,
        sha256=attachment.sha256,
        state=attachment.state,
        revision=attachment.revision,
        created_at=as_utc(attachment.created_at),
    )


def _attachment(attachment_id: UUID) -> Any:
    """The attachment and its spec; anything not found in the current tenant is NotFound."""
    from skeleton.files.models import Attachment

    attachment = (
        Attachment.objects.select_related("target_table")
        .filter(pk=as_uuid(attachment_id, field="attachment_id"))
        .first()
    )
    if attachment is None or not attachment.target_table.model_label:
        raise NotFound()
    return attachment, _spec(attachment.target_table.model_label)


class FileService(ServiceAPI):
    """No direct storage access for apps: bytes go in and out only through these methods and views."""

    @service_method(read_only=False, since="0.5.0")
    def attach(
        self,
        *,
        actor: AbstractBaseUser,
        table_key: str,
        record_id: UUID,
        filename: str,
        stream: BinaryIO,
        content_type: str | None = None,
    ) -> AttachmentInfo:
        """Attach a file to a record; the type is proven by the content, ``content_type`` is advisory.

        Autorização: update no registro alvo (leitura sem update: Forbidden).
        Erros: NotFound para tabela sem opt-in e alvo inexistente, de outra organização ou ilegível;
        Forbidden sem update; InvalidInput para tipo não permitido, nome inválido ou arquivo acima
        do limite; Conflict("quota_exceeded") acima da cota da organização.
        Efeitos: grava os bytes no armazenamento privado e o metadado na transação do chamador.
        """
        from skeleton.files import services

        spec = _spec(table_key)
        try:
            attachment = services.upload(
                spec, as_uuid(record_id, field="record_id"), user=actor, stream=stream, filename=filename
            )
        except services.QuotaExceeded:
            raise Conflict("quota_exceeded") from None
        except services.UploadTooLarge:
            raise InvalidInput({"file": ["O arquivo excede o limite permitido."]}) from None
        except services.UnsupportedFileType:
            raise InvalidInput({"file": ["Tipo de arquivo não permitido."]}) from None
        return _info(attachment)

    @service_method(read_only=True, since="0.5.0")
    def list(
        self,
        *,
        actor: AbstractBaseUser,
        table_key: str,
        record_id: UUID,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[AttachmentInfo]:
        """A page of the record's live attachments, oldest first.

        Autorização: leitura do registro alvo.
        Erros: NotFound para tabela sem opt-in e alvo inexistente, de outra organização, excluído
        ou ilegível; InvalidInput para cursor ou tamanho de página inválidos.
        Efeitos: nenhum.
        """
        from skeleton.files import services

        rows = services.attachments_for(_spec(table_key), as_uuid(record_id, field="record_id"), user=actor)
        return paginate(rows.select_related("target_table"), convert=_info, page_size=page_size, cursor=cursor)

    @service_method(read_only=True, since="0.5.0")
    def download_url(self, *, actor: AbstractBaseUser, attachment_id: UUID) -> str:
        """Return the internal download URL; the view re-authorizes on every request.

        Autorização: leitura do registro alvo e anexo no estado ``available``.
        Erros: NotFound para anexo inexistente, de outra organização, ilegível, pendente ou em
        quarentena.
        Efeitos: nenhum.
        """
        from django.urls import reverse

        from skeleton.files import services
        from skeleton.files.models import Attachment

        attachment, spec = _attachment(attachment_id)
        services.readable_target(spec, attachment.target_sysid, user=actor)
        if attachment.state != Attachment.State.AVAILABLE:
            raise NotFound()
        return reverse(
            "files:download",
            kwargs={
                "organization_id": attachment.organization_id,
                "table_key": spec.model._meta.label_lower,
                "sysid": attachment.target_sysid,
                "attachment_id": attachment.pk,
            },
        )

    @service_method(read_only=False, since="0.5.0")
    def remove(self, *, actor: AbstractBaseUser, attachment_id: UUID, expected_revision: int) -> None:
        """Remove (soft delete) an attachment with CAS.

        Autorização: update no registro alvo.
        Erros: NotFound (inexistente, de outra organização, ilegível); Forbidden sem update;
        Conflict("stale_revision") para revisão desatualizada.
        Efeitos: exclusão lógica auditada; os bytes ficam até a limpeza de órfãos.
        """
        from skeleton.files import services

        if type(expected_revision) is not int:
            raise InvalidInput({"expected_revision": ["Use a revisão inteira do anexo."]})
        attachment, spec = _attachment(attachment_id)
        services.remove(spec, attachment.target_sysid, attachment.pk, user=actor, revision=expected_revision)
