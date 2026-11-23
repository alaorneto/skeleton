"""Clients of the platform API and their ``platform`` tokens (#709).

A platform client is a non-interactive identity (``AccountType.PLATFORM_CLIENT``) with no
organization, no password and no platform role. Its tokens carry only ``platform.*`` scopes,
an expiry and, optionally, the networks they may come from; they open only the views of
``skeleton.api.platform`` (``PlatformAPI``), never an organization, session or administration
view. Only the platform administrator (MFA, recent authentication at the pages) manages them.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

from .scopes import validate_platform_scopes
from .services import IssuedToken, _create, _require_room, _validate_expiry, _validate_name

MAX_NETWORKS = 20


def _require_platform(actor: Any) -> None:
    from skeleton.accounts.platform import require_platform_context

    require_platform_context(actor)


def validate_networks(value: Any) -> list[str]:
    """Normalized CIDRs (at most 20); ``ValidationError`` for anything that is not one."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [item for item in value.replace(",", "\n").split() if item]
    if not isinstance(value, list | tuple) or len(value) > MAX_NETWORKS:
        raise ValidationError({"allowed_networks": gettext("Use até 20 redes CIDR.")})
    result = []
    for item in value:
        try:
            result.append(str(ipaddress.ip_network(str(item).strip(), strict=False)))
        except ValueError:
            raise ValidationError(
                {"allowed_networks": gettext("Rede inválida: %(value)s.") % {"value": str(item)[:60]}}
            ) from None
    return sorted(set(result))


def address_allowed(address: str, networks: list[str]) -> bool:
    if not networks:
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(network) for network in networks)


@transaction.atomic
def create_platform_client(*, actor: Any, name: str) -> Any:
    from skeleton.accounts.models import PLATFORM_CLIENT_EMAIL_DOMAIN, AccountType, User, UserProfile

    _require_platform(actor)
    name = _validate_name(name)
    user = User(
        email=f"client-{uuid4().hex}@{PLATFORM_CLIENT_EMAIL_DOMAIN}",
        first_name=name,
        account_type=AccountType.PLATFORM_CLIENT,
        local_password_allowed=False,
    )
    user.set_unusable_password()
    user.save()
    UserProfile(user=user).save()
    return user


def platform_clients() -> Any:
    from skeleton.accounts.models import AccountType, User

    return User.all_objects.filter(account_type=AccountType.PLATFORM_CLIENT).order_by("first_name", "sysid")


def _client(client_id: Any, *, active: bool = True) -> Any:
    from skeleton.accounts.models import AccountType, User

    queryset = User.objects.select_for_update().filter(pk=client_id, account_type=AccountType.PLATFORM_CLIENT)
    if active:
        queryset = queryset.filter(is_active=True)
    client = queryset.first()
    if client is None:
        raise ValidationError({"client": gettext("Cliente da plataforma indisponível.")})
    return client


@transaction.atomic
def deactivate_platform_client(*, actor: Any, client_id: Any) -> Any:
    """Deactivate the client: every token of it stops at the next request (``auth_version`` bump)."""
    _require_platform(actor)
    client = _client(client_id, active=False)
    if client.is_active:
        client.is_active = False
        client.auth_version += 1
        client.save(update_fields={"is_active", "auth_version"})
    return client


@transaction.atomic
def issue_platform_token(
    *, actor: Any, client_id: Any, name: str, scopes: Any, expires_at: Any, allowed_networks: Any = ()
) -> IssuedToken:
    """A ``platform`` token of an active client; the text is shown once."""
    from .models import ApiToken

    _require_platform(actor)
    client = _client(client_id)
    _require_room(client)
    return _create(
        user=client,
        kind=ApiToken.Kind.PLATFORM,
        name=_validate_name(name),
        scopes=validate_platform_scopes(scopes),
        expires_at=_validate_expiry(expires_at),
        auth_version_at_issue=client.auth_version,
        allowed_networks=validate_networks(allowed_networks),
    )


@transaction.atomic
def revoke_platform_token(*, actor: Any, token_id: Any, expected_revision: int) -> Any:
    from .models import ApiToken

    _require_platform(actor)
    token = ApiToken.objects.select_for_update().filter(pk=token_id, kind=ApiToken.Kind.PLATFORM).first()
    if token is None:
        raise ApiToken.DoesNotExist("Token unavailable.")
    if type(expected_revision) is not int or token.revision != expected_revision:
        raise ConcurrentUpdate("The token changed.")
    token.revoked_at = timezone.now()
    token.save(update_fields={"revoked_at"})
    token.delete()
    return token


def platform_tokens(client_id: Any) -> Any:
    from .models import ApiToken

    return ApiToken.objects.filter(kind=ApiToken.Kind.PLATFORM, user_id=client_id).order_by("-created_at", "sysid")
