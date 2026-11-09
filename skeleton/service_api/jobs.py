"""JobService: enqueue background work of the calling app's own job types (#108)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser

from .applications import calling_app_label
from .base import ServiceAPI, service_method
from .dto import as_utc
from .errors import Forbidden, InvalidInput

Scalar = str | int | bool


@dataclass(frozen=True, slots=True)
class JobReceipt:
    """Identity and state of the queued job; never the payload back."""

    sysid: UUID
    job_type: str
    state: str
    run_after: datetime
    created: bool


class JobService(ServiceAPI):
    """Job types are registered in ``ready()``; only the app that registered a type may use it."""

    @service_method(read_only=False, since="0.5.0", allow_system=True)
    def enqueue(
        self,
        *,
        actor: AbstractBaseUser | None,
        job_type: str,
        payload: tuple[tuple[str, Scalar], ...] = (),
        idempotency_key: str | None = None,
        run_after: datetime | None = None,
    ) -> JobReceipt:
        """Enqueue a job of the calling app for the current organization, in the caller's transaction.

        Autorização: o tipo pertence à app chamadora (``owner_app``); a organização é sempre a do
        contexto. Jobs ``run_as="actor"`` rodam como o ator, que o worker revalida.
        Erros: Forbidden para tipo de outra app; InvalidInput para tipo desconhecido, payload
        inválido (chave extra, tipo errado, acima de 4 KB) ou ``run_after`` sem fuso.
        Efeitos: grava um job; o rollback do chamador desfaz. A chave de idempotência repetida
        devolve o job existente (``created=False``).
        """
        from skeleton.jobs.registry import get_job_type
        from skeleton.jobs.services import enqueue
        from skeleton.tenancy.context import current_context

        try:
            kind = get_job_type(job_type)
        except KeyError:
            raise InvalidInput({"job_type": ["Tipo de job desconhecido."]}) from None
        if calling_app_label() != (kind.owner_app or None):
            raise Forbidden("A app só enfileira os próprios tipos de job.")
        receipt = enqueue(
            kind.code,
            organization_id=current_context().organization_id,
            payload=dict(payload),
            actor=actor,
            idempotency_key=idempotency_key,
            run_after=run_after,
        )
        return JobReceipt(receipt.id, receipt.job_type, receipt.state, as_utc(receipt.run_after), receipt.created)
