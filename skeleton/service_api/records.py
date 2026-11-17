"""RecordSealService: seal, read and verify the seal of a record (#527).

Sealing is the business act of an app (signing, posting); this service is how another app seals
or verifies a record of a sealable table it can update or read. An app sealing its own models in
process may call ``skeleton.records.sealing.seal_record`` instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext

from .base import ServiceAPI, service_method
from .dto import as_utc, as_uuid
from .errors import Conflict, Forbidden, InvalidInput, NotFound, Unavailable

__all__ = ["RecordSealService", "SealInfo", "SealVerification"]
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class SealInfo:
    sysid: UUID
    sealed: bool
    sealed_at: datetime | None
    sealed_by: UUID | None
    digest: str
    revision: int


@dataclass(frozen=True, slots=True)
class SealVerification:
    sysid: UUID
    valid: bool
    digest: str
    computed: str


def _info(record: Any) -> SealInfo:
    return SealInfo(
        sysid=record.pk,
        sealed=record.sealed_at is not None,
        sealed_at=as_utc(record.sealed_at) if record.sealed_at else None,
        sealed_by=record.sealed_by,
        digest=record.seal_digest,
        revision=record.revision,
    )


def _model(table_key: object) -> Any:
    from skeleton.access.derivation import is_authorizable
    from skeleton.common.models import SealableMixin
    from skeleton.system.registry import get_spec

    if not isinstance(table_key, str):
        raise InvalidInput({"table_key": [gettext("Use o identificador da tabela.")]})
    try:
        spec = get_spec(table_key)
    except KeyError:
        spec = None
    if (
        spec is None
        or not is_authorizable(spec.model)  # Exposed or derived (#564).
        or not issubclass(spec.model, SealableMixin)
        or spec.seal is None
    ):
        raise InvalidInput({"table_key": [gettext("Tabela desconhecida ou sem selo.")]})
    return spec.model


def _readable(actor: Any, model: Any, record_id: Any) -> Any:
    from skeleton.access.policies import authorized_queryset
    from skeleton.system.applications import require_application_enabled

    identifier = as_uuid(record_id, field="record_id")
    try:
        require_application_enabled(model._meta.app_label)
    except PermissionDenied:
        raise Unavailable() from None  # The table's app is unavailable or disabled in this organization.
    try:
        record = authorized_queryset(model, user=actor, action="read").filter(pk=identifier).first()
    except PermissionDenied:
        record = None
    if record is None:
        raise NotFound()
    return record


def _extra(extra_digest: object) -> dict[str, str] | None:
    if extra_digest in ("", None):
        return None
    if not isinstance(extra_digest, str) or not _DIGEST.fullmatch(extra_digest):
        raise InvalidInput({"extra_digest": [gettext("Use um SHA-256 em hexadecimal minúsculo.")]})
    return {"digest": extra_digest}


class RecordSealService(ServiceAPI):
    """The seal of exposed or derived tables that declare ``TableSpec.seal``; the core signs nothing."""

    @service_method(read_only=False, since="0.6.0")
    def seal(
        self,
        *,
        actor: AbstractBaseUser,
        table_key: str,
        record_id: UUID,
        expected_revision: int,
        extra_digest: str = "",
    ) -> SealInfo:
        """Seal the record once: its frozen columns, the seal and its deletion never change again.

        Autorização: ``update`` no registro.
        Erros: NotFound para registro inexistente, de outra organização ou ilegível; Unavailable com a
        app da tabela desativada; Forbidden para
        quem lê e não altera; Conflict("record_sealed") se já selado; Conflict("stale_revision")
        para revisão divergente; InvalidInput para tabela sem selo, revisão ou ``extra_digest``
        inválidos.
        Efeitos: grava o selo (``sealed_at``, ``sealed_by`` e o resumo SHA-256 dos campos
        congelados, dos anexos congelados e de ``extra_digest``, o resumo que a app calculou das
        partes dela com ``skeleton.common.sealing.digest``) na transação do chamador, auditado.
        """
        from skeleton.common.exceptions import ConcurrentUpdate, RecordSealed
        from skeleton.records.sealing import seal_record

        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidInput({"expected_revision": [gettext("Informe a revisão atual.")]})
        model = _model(table_key)
        extra = _extra(extra_digest)
        record = _readable(actor, model, record_id)
        try:
            return _info(seal_record(record, user=actor, expected_revision=expected_revision, extra=extra))
        except PermissionDenied:
            raise Forbidden() from None
        except RecordSealed:
            raise Conflict("record_sealed") from None
        except ConcurrentUpdate:
            raise Conflict("stale_revision") from None

    @service_method(read_only=True, since="0.6.0")
    def status(self, *, actor: AbstractBaseUser, table_key: str, record_id: UUID) -> SealInfo:
        """Whether the record is sealed, by whom, when, and its digest.

        Autorização: leitura do registro.
        Erros: NotFound para registro inexistente, de outra organização ou ilegível; InvalidInput
        para tabela sem selo.
        Efeitos: nenhum.
        """
        return _info(_readable(actor, _model(table_key), record_id))

    @service_method(read_only=True, since="0.6.0")
    def verify(
        self, *, actor: AbstractBaseUser, table_key: str, record_id: UUID, extra_digest: str = ""
    ) -> SealVerification:
        """Recompute the digest from the stored content (columns and attachments) and compare.

        Autorização: leitura do registro.
        Erros: NotFound como em ``status``; InvalidInput para tabela sem selo ou ``extra_digest``
        inválido (passe o mesmo usado ao selar).
        Efeitos: nenhum. ``valid`` é falso para registro não selado.
        """
        from skeleton.common.sealing import digest
        from skeleton.records.sealing import seal_payload

        model = _model(table_key)
        extra = _extra(extra_digest)
        record = _readable(actor, model, record_id)
        computed = digest(seal_payload(record, extra=extra))
        return SealVerification(
            sysid=record.pk,
            valid=record.sealed_at is not None and computed == record.seal_digest,
            digest=record.seal_digest,
            computed=computed,
        )
