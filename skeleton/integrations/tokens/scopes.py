"""Token scopes (#45): what a token may do, always intersected with the user's role.

Two forms are accepted:

- ``<table_key>:<action>`` for a table exposed by the generic API (``TableSpec.expose_api``)
  and one of ``read``, ``create``, ``update``, ``delete`` or ``restore``;
- a named scope registered in code with :func:`register_token_scope`, such as
  ``files:read`` or ``calendar.feed``. A ``feed`` scope is only valid on feed tokens, and a
  feed token accepts only feed scopes.

A scope never grants anything by itself: the request still needs the user's table
permission, record visibility and app activation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import Model

import re
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

TABLE_ACTIONS = ("read", "create", "update", "delete", "restore")
MAX_SCOPES = 50
_SCOPE = re.compile(r"^[a-z0-9_.]{1,100}:[a-z_]{1,32}$|^[a-z0-9_.]{1,100}$")


@dataclass(frozen=True)
class TokenScope:
    name: str
    description: str
    feed: bool = False


_named: dict[str, TokenScope] = {}


def register_token_scope(name: str, description: str, *, feed: bool = False) -> TokenScope:
    """Declare a named scope in ``AppConfig.ready()``."""
    scope = TokenScope(name, description, feed)
    if not _SCOPE.match(name):
        raise ValueError(f"Invalid scope name: {name}")
    if name in _named and _named[name] != scope:
        raise ValueError(f"Duplicate token scope: {name}")
    _named[name] = scope
    return scope


def named_scopes() -> tuple[TokenScope, ...]:
    return tuple(_named[name] for name in sorted(_named))


def table_scopes() -> tuple[str, ...]:
    from skeleton.system.registry import all_specs

    return tuple(
        f"{spec.model._meta.label_lower}:{action}"
        for spec in all_specs()
        if spec.expose_api
        for action in TABLE_ACTIONS
    )


def table_scope(model: type[Model], action: str) -> str:
    return f"{model._meta.label_lower}:{action}"


def validate_scopes(scopes: object, *, feed: bool) -> list[str]:
    """Normalized, sorted and unique scopes, or ``ValidationError`` (400) for any unknown one."""
    if not isinstance(scopes, list | tuple) or not scopes:
        raise ValidationError({"scopes": gettext("Informe ao menos um escopo.")})
    if len(scopes) > MAX_SCOPES or not all(isinstance(scope, str) for scope in scopes):
        raise ValidationError(
            {"scopes": gettext("Use até %(MAX_SCOPES)s escopos em texto.") % {"MAX_SCOPES": MAX_SCOPES}}
        )
    known_tables = set(table_scopes())
    clean = sorted(set(scopes))
    for scope in clean:
        named = _named.get(scope)
        if named is not None:
            if named.feed != feed:
                raise ValidationError(
                    {"scopes": gettext("Escopo %(scope)s não vale para este tipo de token.") % {"scope": scope}}
                )
        elif feed or scope not in known_tables:
            raise ValidationError(
                {"scopes": gettext("Escopo desconhecido ou de tabela não exposta: %(scope)s.") % {"scope": scope}}
            )
    return clean
