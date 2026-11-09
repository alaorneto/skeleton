"""AccessService: authorization questions answered by the core policies, never a parallel path (#104)."""

from __future__ import annotations

from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied

from .base import ServiceAPI, service_method
from .dto import as_uuid
from .errors import Forbidden, InvalidInput, NotFound
from .organizations import GroupSummary


def _model(table_key: str):
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import get_spec

    if not isinstance(table_key, str):
        raise InvalidInput({"table_key": ["Use o identificador da tabela."]})
    try:
        spec = get_spec(table_key)
    except KeyError:
        return None
    if not spec.expose_api or not issubclass(spec.model, SecuredRecord):
        return None
    return spec.model


def _action(action: str) -> str:
    from skeleton.access.policies import ACTIONS

    if action not in ACTIONS:
        raise InvalidInput({"action": [f"Use uma de: {', '.join(sorted(ACTIONS))}."]})
    return action


def _decide(actor, action: str, table_key: str, record_id) -> tuple[bool, bool]:
    """(readable, allowed): readability decides NotFound, the action decides Forbidden."""
    from skeleton.access.policies import authorized_queryset

    action = _action(action)
    model = _model(table_key)
    if model is None:
        return False, False
    if record_id is None:
        try:
            authorized_queryset(model, user=actor, action=action)
        except PermissionDenied:
            return True, False
        return True, True
    identifier = as_uuid(record_id, field="record_id")

    def exists(name: str) -> bool:
        try:
            return authorized_queryset(model, user=actor, action=name).filter(pk=identifier).exists()
        except PermissionDenied:
            return False

    # ``allowed`` is exactly authorized_queryset of the action; reading only decides NotFound/Forbidden.
    allowed = exists(action)
    readable = allowed or (action != "read" and exists("read"))
    return readable, allowed


class AccessService(ServiceAPI):
    """Consistent with ``authorized_queryset``/``require_record_action``; no cache between calls."""

    @service_method(read_only=True, since="0.5.0")
    def can(self, *, actor: AbstractBaseUser, action: str, table_key: str, record_id: UUID | None = None) -> bool:
        """Whether the actor may perform ``action`` on the table or on one record.

        Autorização: as mesmas camadas de ``authorized_queryset`` (vínculo, TablePermission,
        visibilidade, concessões, ativação da app), recalculadas a cada chamada.
        Erros: InvalidInput para ação ou identificador inválidos. Tabela desconhecida, registro
        inexistente ou de outra organização respondem ``False``, como um registro sem acesso.
        Efeitos: nenhum.
        """
        return _decide(actor, action, table_key, record_id)[1]

    @service_method(read_only=True, since="0.5.0")
    def require(self, *, actor: AbstractBaseUser, action: str, table_key: str, record_id: UUID | None = None) -> None:
        """Raise unless the actor may perform ``action``.

        Autorização: igual a ``can``.
        Erros: NotFound se o registro (ou a tabela) não existir ou não for legível, inclusive de
        outra organização; Forbidden se for legível mas a ação for negada; InvalidInput.
        Efeitos: nenhum.
        """
        readable, allowed = _decide(actor, action, table_key, record_id)
        if not readable:
            raise NotFound()
        if not allowed:
            raise Forbidden()

    @service_method(read_only=True, since="0.5.0")
    def capabilities(self, *, actor: AbstractBaseUser) -> tuple[str, ...]:
        """Return the actor's organizational capabilities, sorted.

        Autorização: vínculo ativo; capacidades vêm só de papéis internos (direto ou por grupo).
        Erros: nenhum além do contrato do contexto.
        Efeitos: nenhum.
        """
        from skeleton.access.capabilities import capabilities_for

        return tuple(sorted(capabilities_for(actor)))

    @service_method(read_only=True, since="0.5.0")
    def my_groups(self, *, actor: AbstractBaseUser) -> tuple[GroupSummary, ...]:
        """Return the live groups the actor belongs to in the current organization.

        Autorização: o próprio ator.
        Erros: Forbidden sem vínculo ativo.
        Efeitos: nenhum.
        """
        from skeleton.access.models import AccessGroup
        from skeleton.access.policies import group_ids

        groups = AccessGroup.objects.filter(pk__in=group_ids(actor)).order_by("code", "sysid")
        return tuple(GroupSummary(group.pk, group.code, group.name) for group in groups)
