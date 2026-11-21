"""SequenceService: gapless commercial numbers for apps (#155)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.contrib.auth.base_user import AbstractBaseUser
from django.utils.translation import gettext

from .applications import require_owning_app
from .base import ServiceAPI, service_method
from .errors import InvalidInput

__all__ = ["SequenceNumber", "SequenceService"]


@dataclass(frozen=True, slots=True)
class SequenceNumber:
    key: str
    period: str
    number: int
    formatted: str


class SequenceService(ServiceAPI):
    """Sequences stay declared by each app; only the owning app draws numbers from its own."""

    @service_method(read_only=False, since="0.1.0", allow_system=True)
    def next_number(self, *, actor: AbstractBaseUser | None, key: str, at: datetime | None = None) -> SequenceNumber:
        """The next number of ``key`` in the actor's organization and the period of ``at`` (now).

        Autorização: a app chamadora precisa ser a dona da sequência (``owner_app``); o número é da
        organização do contexto, sem leitura de registros. ``actor=None`` só em ``system_context``
        com organização (o de um consumidor de evento ou de um job por organização, desde 0.2.0,
        #577); a regra da app dona vale igual.
        Erros: Forbidden para sequência de outra app; InvalidInput para sequência não declarada ou
        data sem fuso.
        Efeitos: incrementa o contador na transação do chamador; o rollback dela devolve o número,
        e a numeração não tem lacunas. Alocações da mesma sequência e período são serializadas.
        """
        from django.core.exceptions import ValidationError

        from skeleton.sequences.registry import get_sequence
        from skeleton.sequences.services import next_number

        try:
            spec = get_sequence(key)
        except KeyError:
            raise InvalidInput({"key": [gettext("Sequência não declarada.")]}) from None
        require_owning_app(spec.owner_app, gettext("A app só numera com as próprias sequências."))
        try:
            allocated = next_number(spec.key, at=at)
        except ValidationError as exc:
            raise InvalidInput({"at": [str(message) for message in exc.messages]}) from None
        return SequenceNumber(allocated.key, allocated.period, allocated.number, allocated.formatted)
