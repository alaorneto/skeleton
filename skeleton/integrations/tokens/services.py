"""Issue, revoke and verify API tokens (#45).

The token text ``skt_<prefix>_<secret>`` is returned once by :func:`issue_token` and never
stored: the row keeps the prefix and the SHA-256 of the secret. With 256 bits of entropy a
plain SHA-256 is enough (no slow hash), and the comparison is constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context

from .scopes import validate_scopes

TOKEN_PATTERN = re.compile(r"^skt_([0-9a-f]{8})_([A-Za-z0-9_-]{43})$")
DEFAULT_MAX_DAYS = 365


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """The only object that ever carries the secret; show ``text`` once and drop it."""

    token: Any
    text: str


def max_days() -> int:
    return int(getattr(settings, "SKELETON_API_TOKEN_MAX_DAYS", DEFAULT_MAX_DAYS))


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _validate_expiry(expires_at: Any) -> datetime:
    if not isinstance(expires_at, datetime) or timezone.is_naive(expires_at):
        raise ValidationError({"expires_at": gettext("Informe a validade com data, hora e fuso.")})
    now = timezone.now()
    if expires_at <= now:
        raise ValidationError({"expires_at": gettext("A validade precisa estar no futuro.")})
    if expires_at > now + timedelta(days=max_days()):
        raise ValidationError(
            {"expires_at": gettext("A validade máxima é de %(value1)s dias.") % {"value1": max_days()}}
        )
    return expires_at


def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 150:
        raise ValidationError({"name": gettext("Informe um nome de até 150 caracteres.")})
    return name.strip()


def _require_mfa_policy(actor: Any) -> None:
    from skeleton.accounts.mfa import mfa_required

    if mfa_required(actor):
        raise PermissionDenied(gettext("Ative a verificação em duas etapas para emitir tokens."))


def _require_session_actor(actor: Any) -> None:
    context = current_context()
    if not getattr(actor, "is_authenticated", False) or context.actor != actor.pk:
        raise PermissionDenied("The actor must be the authenticated user.")
    if getattr(context, "credential_scopes", None) is not None:
        raise PermissionDenied(gettext("Um token não pode gerenciar tokens."))


def _create(**values: Any) -> IssuedToken:
    from .models import ApiToken

    for _ in range(3):
        prefix, secret = secrets.token_hex(4), secrets.token_urlsafe(32)
        token = ApiToken(prefix=prefix, digest=digest(secret), **values)
        try:
            with transaction.atomic(using=current_context().using):
                token.save()
        except IntegrityError as exc:
            if "sk_integrations_apitoken_prefix" not in str(exc):
                raise  # Another constraint is a real error, not a collision (#327).
            continue  # Prefix collision (1 in 4 billion): draw again.
        return IssuedToken(token, f"skt_{prefix}_{secret}")
    raise RuntimeError("Could not allocate a token prefix.")


DEFAULT_MAX_PER_OWNER = 50


def _require_room(user: Any) -> None:
    """At most ``SKELETON_API_TOKEN_MAX_PER_OWNER`` live tokens per person or account (#327).

    Expired tokens count until revoked: they still show in the list. Personal issues of the
    same person at the same instant may pass the cap by one (no lock on the user); service
    tokens are serialized by the organization lock.
    """
    from .models import ApiToken

    limit = int(getattr(settings, "SKELETON_API_TOKEN_MAX_PER_OWNER", DEFAULT_MAX_PER_OWNER))
    if ApiToken.objects.filter(user=user).count() >= limit:
        raise ValidationError(
            {
                "name": gettext(
                    "Limite de %(limit)s tokens atingido: revogue um token (inclusive os expirados) antes de criar outro."
                )
                % {"limit": limit}
            }
        )


def issue_personal_token(*, actor: Any, name: Any, scopes: Any, expires_at: Any, feed: bool = False) -> IssuedToken:
    """A token of the actor itself, global (all of the actor's organizations)."""
    from .models import ApiToken

    _require_session_actor(actor)
    _require_mfa_policy(actor)
    if getattr(actor, "account_type", "human") != "human":
        raise PermissionDenied(gettext("Contas de serviço recebem tokens pela organização."))
    _require_room(actor)
    return _create(
        user=actor,
        kind=ApiToken.Kind.FEED if feed else ApiToken.Kind.PERSONAL,
        bound_organization=None,
        name=_validate_name(name),
        scopes=validate_scopes(scopes, feed=feed),
        expires_at=_validate_expiry(expires_at),
        auth_version_at_issue=actor.auth_version,
    )


def issue_service_token(*, actor: Any, account: Any, name: Any, scopes: Any, expires_at: Any) -> IssuedToken:
    """A token of a service account of the current organization (``integrations.manage``)."""
    from skeleton.access.capabilities import INTEGRATIONS_MANAGE
    from skeleton.access.services import organization_lock
    from skeleton.accounts.models import AccountType, User

    from .models import ApiToken

    _require_session_actor(actor)
    _require_mfa_policy(actor)
    with organization_lock(actor, capability=INTEGRATIONS_MANAGE) as organization:
        user = (
            User.objects.select_for_update()
            .filter(pk=getattr(account, "pk", account), account_type=AccountType.SERVICE)
            .filter(service_organization=organization, is_active=True)
            .first()
        )
        if user is None:
            raise ValidationError({"account": gettext("Conta de serviço indisponível.")})
        _require_room(user)
        return _create(
            user=user,
            kind=ApiToken.Kind.SERVICE,
            bound_organization=organization,
            name=_validate_name(name),
            scopes=validate_scopes(scopes, feed=False),
            expires_at=_validate_expiry(expires_at),
            auth_version_at_issue=user.auth_version,
        )


def _manageable(token: Any, actor: Any) -> bool:
    from skeleton.access.capabilities import INTEGRATIONS_MANAGE, has_capability

    if token.kind == token.Kind.SERVICE:
        return current_context().organization_id == token.bound_organization_id and has_capability(
            actor, INTEGRATIONS_MANAGE
        )
    return token.user_id == actor.pk


def revoke_token(*, actor: Any, token_id: Any, expected_revision: int) -> Any:
    """Revoke (``revoked_at`` + soft delete); the next request with the token gets 401.

    A token the actor may not manage is reported as missing, like a nonexistent one.
    """
    from .models import ApiToken

    _require_session_actor(actor)
    with transaction.atomic(using=current_context().using):
        token = ApiToken.objects.select_for_update().filter(pk=token_id).first()
        if token is None or not _manageable(token, actor):
            raise ApiToken.DoesNotExist("Token unavailable.")
        if type(expected_revision) is not int or token.revision != expected_revision:
            raise ConcurrentUpdate("The token changed.")
        token.revoked_at = timezone.now()
        token.save(update_fields={"revoked_at"})
        token.delete()
        return token


def personal_tokens(user: Any):
    from .models import ApiToken

    return (
        ApiToken.objects.filter(user=user, kind__in=(ApiToken.Kind.PERSONAL, ApiToken.Kind.FEED))
        .select_related("user")
        .order_by("-created_at", "sysid")
    )


def service_tokens(organization_id: Any, *, account: Any = None):
    from .models import ApiToken

    queryset = ApiToken.objects.filter(kind=ApiToken.Kind.SERVICE, bound_organization_id=organization_id)
    if account is not None:
        queryset = queryset.filter(user_id=getattr(account, "pk", account))
    return queryset.select_related("user").order_by("-created_at", "sysid")


def is_usable(token: Any, *, now: Any = None) -> bool:
    """Whether ``verify_token`` would still accept this token (the secret aside) (#327)."""
    user = token.user
    return not (
        token.revoked_at is not None
        or token.expires_at <= (now or timezone.now())
        or not user.is_active
        or user.deleted_at is not None
        or user.auth_version != token.auth_version_at_issue
    )


def verify_token(text: str) -> Any | None:
    """The live token for ``text``, or ``None``: malformed, unknown, wrong secret, expired,
    revoked, user inactive or ``auth_version`` changed since issue all look the same."""
    from .models import ApiToken

    match = TOKEN_PATTERN.match(text or "")
    if match is None:
        return None
    prefix, secret = match.groups()
    token = ApiToken.objects.select_related("user").filter(prefix=prefix).first()
    if token is None or not hmac.compare_digest(token.digest, digest(secret)):
        return None
    return token if is_usable(token) else None
