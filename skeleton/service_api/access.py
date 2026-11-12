"""AccessService: authorization questions answered by the core policies, never a parallel path (#104)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext

from .base import ServiceAPI, service_method
from .dto import as_uuid
from .errors import Forbidden, InvalidInput, NotFound
from .identity import _user
from .organizations import GroupSummary


def _model(table_key: str) -> Any:
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import get_spec

    if not isinstance(table_key, str):
        raise InvalidInput({"table_key": [gettext("Use o identificador da tabela.")]})
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
        raise InvalidInput({"action": [gettext("Use uma de: %(value1)s.") % {"value1": ", ".join(sorted(ACTIONS))}]})
    return action


def _decide(actor: Any, action: str, table_key: str, record_id: Any) -> tuple[bool, bool]:
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

        return tuple(sorted(capabilities_for(_user(actor))))

    @service_method(read_only=True, since="0.5.0")
    def my_groups(self, *, actor: AbstractBaseUser) -> tuple[GroupSummary, ...]:
        """Return the live groups the actor belongs to in the current organization.

        Autorização: o próprio ator.
        Erros: Forbidden sem vínculo ativo.
        Efeitos: nenhum.
        """
        from skeleton.access.models import AccessGroup
        from skeleton.access.policies import group_ids

        groups = AccessGroup.objects.filter(pk__in=group_ids(_user(actor))).order_by("code", "sysid")
        return tuple(GroupSummary(group.pk, group.code, group.name) for group in groups)

    @service_method(read_only=False, since="0.5.0")
    def grant_member_table_access(
        self, *, actor: AbstractBaseUser, table_key: str, actions: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Give the built-in ``member`` role ``actions`` on an exposed table; return what was added.

        Autorização: ``access.manage`` (administrador da organização), pelo mesmo serviço de
        concessão da gestão de acesso (lock da organização e auditoria). Ações que o papel já
        tem são puladas, então repetir não muda nada.
        Erros: InvalidInput para tabela não exposta ou ação inválida; Forbidden sem
        ``access.manage``.
        Efeitos: grava ``TablePermission`` na transação do chamador.
        """
        from skeleton.access.capabilities import ACCESS_MANAGE, has_capability
        from skeleton.access.models import Role, TablePermission
        from skeleton.access.policies import table_definition
        from skeleton.access.services import grant_table_permission

        if not has_capability(_user(actor), ACCESS_MANAGE):
            raise Forbidden()
        model = _model(table_key)
        if model is None:
            raise InvalidInput({"table_key": [gettext("Tabela desconhecida ou não exposta.")]})
        if not isinstance(actions, tuple) or not actions:
            raise InvalidInput({"actions": [gettext("Informe as ações.")]})
        for action in actions:
            _action(action)
        table, role = table_definition(model), Role.objects.filter(code="member", is_builtin=True).first()
        if table is None or role is None:
            raise InvalidInput({"table_key": [gettext("Catálogo ou papel member indisponível.")]})
        granted = []
        for action in dict.fromkeys(actions):
            if not TablePermission.objects.filter(role=role, table=table, action=action).exists():
                grant_table_permission(user=_user(actor), data={"role": role, "table": table, "action": action})
                granted.append(action)
        return tuple(granted)
