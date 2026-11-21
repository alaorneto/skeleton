"""OIDC access tokens of native clients on ``/api/v1/`` (#604).

``ApiTokenMiddleware`` stays the single Bearer authentication point: a Bearer value that is not
``skt_...`` is looked up here, by its SHA-256 in allauth's token table (the unique ``(type,
hash)`` index), as a live access token of an enabled native client whose person is active and
kept the ``auth_version`` stamped at issue. The result carries the same attributes the core
reads from an ``ApiToken`` (``user``, ``scopes``, ``kind``, ``bound_organization_id``, ``pk``,
``audit_source``), so every rule of the personal tokens applies unchanged.

Scopes: ``openid profile email`` alone give a **broad personal token**: every table action and
named non-feed scope of the core, still intersected with the person's role, never an
administrative capability (``capabilities_for`` is empty under any credential scopes). When the
operator registered core scopes for the client and the client asked for them, only those apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings


@dataclass(frozen=True)
class OidcCredential:
    token: Any
    user: Any
    client_id: str
    scopes: tuple[str, ...]
    kind = "oidc"
    bound_organization_id = None

    @property
    def pk(self) -> str:
        return f"oidc:{self.token.pk}"

    @property
    def audit_source(self) -> str:
        """The GUC ``skeleton.source`` of the request: the audit names the native client."""
        return f"http:oidc:{self.client_id}"


def broad_scopes() -> tuple[str, ...]:
    """Every table action and named non-feed scope: the role decides, the scope never widens it."""
    from .scopes import named_scopes, table_scopes

    return tuple(sorted({*table_scopes(), *(scope.name for scope in named_scopes() if not scope.feed)}))


def core_scopes(granted: list[str]) -> tuple[str, ...]:
    from skeleton.accounts.idp.clients import OIDC_SCOPES

    fine = sorted({scope for scope in granted if scope not in OIDC_SCOPES})
    return tuple(fine) if fine else broad_scopes()


def enabled() -> bool:
    from skeleton.accounts.idp.clients import provider_enabled

    return bool(getattr(settings, "SKELETON_OIDC_API_TOKENS", True)) and provider_enabled()


def verify_oidc_token(text: str) -> OidcCredential | None:
    """The credential of a live, stamped access token; ``None`` for anything else (all look the same)."""
    from allauth.idp.oidc.models import Token

    from skeleton.accounts.idp import clients

    if not enabled():
        return None
    token = clients.lookup(text, types=(Token.Type.ACCESS_TOKEN,))
    if token is None or not clients.usable(token):
        return None
    return OidcCredential(
        token=token, user=token.user, client_id=token.client_id, scopes=core_scopes(token.get_scopes())
    )


def touch(credential: OidcCredential) -> None:
    from skeleton.accounts.idp.clients import touch as touch_token

    touch_token(credential.token)
