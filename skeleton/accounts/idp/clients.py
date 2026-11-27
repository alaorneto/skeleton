"""Registry of native clients and the lifecycle of their tokens (#603).

A native client is a **public** OIDC client: no secret, Authorization Code with PKCE ``S256``,
redirects only to a loopback IP (RFC 8252 §7.3: any port, exact path), the grants
``authorization_code`` and ``refresh_token`` and the scopes ``openid profile email`` plus the
core scopes the operator registered for it.

Every access and refresh token allauth issues to a native client carries, in its ``data``, the
``skeleton`` stamp: the grant it belongs to (one sign-in on one device, kept across rotations),
when that grant started, the person's ``auth_version`` at issue and the last use. A token whose
stamp is missing or whose ``auth_version`` changed (password, MFA, deactivation, session
revocation) is refused everywhere, exactly like a ``skt_`` token. Revoking a device deletes the
tokens of its grant; revoking a client deletes all of them.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

OIDC_SCOPES = ("openid", "profile", "email")
GRANT_TYPES = ("authorization_code", "refresh_token")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
CLIENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
META = "skeleton"
TOUCH_WINDOW_S = 300


# --------------------------------------------------------------------------- provider state


def provider_enabled() -> bool:
    """The provider answers only with a signing key configured (``IDP_OIDC_PRIVATE_KEY*``)."""
    from django.conf import settings

    return bool(getattr(settings, "IDP_OIDC_PRIVATE_KEY", "") or getattr(settings, "IDP_OIDC_PRIVATE_KEYS", []))


def canonical_origin(request: Any) -> str:
    """``SKELETON_PUBLIC_URL`` when set, else the request's own origin (behind the trusted proxy).

    On an organization host (``<slug>.<base>``) the public URL is the platform host of the base
    domain, so the issuer stays the same whichever host the client used to discover it.
    """
    from django.conf import settings

    public = urlsplit((getattr(settings, "SKELETON_PUBLIC_URL", "") or "").strip())
    if public.scheme in {"http", "https"} and public.netloc:
        return f"{public.scheme}://{public.netloc}"
    return request.build_absolute_uri("/").rstrip("/")


def issuer(request: Any) -> str | None:
    """The issuer of the provider, or ``None`` while it is off."""
    return canonical_origin(request) if provider_enabled() else None


# --------------------------------------------------------------------------- registry


def validate_redirect_uri(value: Any) -> str:
    """``http://127.0.0.1/<path>`` or ``http://[::1]/<path>``: loopback IP, no port, no query."""
    if not isinstance(value, str) or len(value) > 500:
        raise ValidationError({"redirect_uri": "Informe um redirect loopback."})
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValidationError({"redirect_uri": "Redirect inválido."}) from None
    if (
        parts.scheme != "http"
        or parts.hostname not in LOOPBACK_HOSTS
        or parts.netloc not in {"127.0.0.1", "[::1]"}
        or port is not None
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not parts.path.startswith("/")
        or "\\" in value
    ):
        raise ValidationError(
            {"redirect_uri": "Use http://127.0.0.1/<caminho> ou http://[::1]/<caminho>, sem porta nem query."}
        )
    return value


def redirect_uri_allowed(value: Any, registered: list[str]) -> bool:
    """The RFC 8252 match: a registered loopback URI with the same host and path, on any port."""
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return False
    if parts.scheme != "http" or parts.username or parts.password or parts.fragment or parts.query:
        return False
    for uri in registered:
        allowed = urlsplit(uri)
        if allowed.hostname == parts.hostname and allowed.path == parts.path:
            return True
    return False


def _extra_scopes(scopes: Any) -> list[str]:
    if not scopes:
        return []
    from skeleton.integrations.tokens.scopes import validate_scopes

    try:
        return validate_scopes(list(scopes), feed=False)
    except ValidationError as exc:
        detail = exc.message_dict.get("scopes", exc.messages) if hasattr(exc, "error_dict") else exc.messages
        raise ValidationError({"scope": detail}) from None


def register_client(
    *, client_id: str, name: str, redirect_uris: list[str], skip_consent: bool = False, scopes: Any = ()
) -> Any:
    """Create the ``NativeClient`` and its public allauth ``Client`` together (call in a system context)."""
    from allauth.idp.oidc.models import Client

    from skeleton.accounts.models import NativeClient

    if not isinstance(client_id, str) or not CLIENT_ID.fullmatch(client_id):
        raise ValidationError({"client_id": "Use de 2 a 64 caracteres: minúsculas, dígitos, ponto, hífen ou _."})
    name = (name or "").strip()
    if not name or len(name) > 100:
        raise ValidationError({"name": "Informe um nome de até 100 caracteres."})
    if not redirect_uris:
        raise ValidationError({"redirect_uri": "Informe ao menos um redirect."})
    uris = sorted({validate_redirect_uri(uri) for uri in redirect_uris})
    extra = _extra_scopes(scopes)
    with transaction.atomic():
        if (
            NativeClient.all_objects.filter(client_id=client_id).exists()
            or Client.objects.filter(id=client_id).exists()
        ):
            raise ValidationError({"client_id": "Já existe um cliente com este identificador."})
        record = NativeClient.objects.create(
            client_id=client_id, name=name, redirect_uris=uris, scopes=extra, skip_consent=bool(skip_consent)
        )
        client = Client(id=client_id, name=name, type=Client.Type.PUBLIC, skip_consent=bool(skip_consent))
        client.set_scopes([*OIDC_SCOPES, *extra])
        client.set_default_scopes(list(OIDC_SCOPES))
        client.set_grant_types(list(GRANT_TYPES))
        client.set_response_types(["code"])
        client.set_redirect_uris(uris)
        client.save()
    return record


def clients() -> list[Any]:
    from skeleton.accounts.models import NativeClient

    return list(NativeClient.objects.order_by("client_id"))


def active_client(client_id: Any) -> Any | None:
    """The enabled ``NativeClient`` for ``client_id``; ``None`` for unknown, disabled or deleted."""
    from skeleton.accounts.models import NativeClient

    if not isinstance(client_id, str) or not CLIENT_ID.fullmatch(client_id):
        return None
    return NativeClient.objects.filter(client_id=client_id, enabled=True).first()


def revoke_all(*, client_id: str, disable: bool = False) -> int:
    """Delete every token of the client (audited on ``NativeClient``); ``disable`` also closes it."""
    from allauth.idp.oidc.models import Token

    from skeleton.accounts.models import NativeClient

    with transaction.atomic():
        record = NativeClient.objects.select_for_update().filter(client_id=client_id).first()
        if record is None:
            raise ValidationError({"client_id": "Cliente desconhecido."})
        deleted, _ = Token.objects.filter(client_id=client_id).delete()
        record.tokens_revoked_at = timezone.now()
        fields = ["tokens_revoked_at"]
        if disable and record.enabled:
            record.enabled = False
            fields.append("enabled")
        record.save(update_fields=fields)
    return deleted


# --------------------------------------------------------------------------- token stamps


def meta(token: Any) -> dict[str, Any] | None:
    data = token.data if isinstance(token.data, dict) else None
    value = data.get(META) if data else None
    return value if isinstance(value, dict) and isinstance(value.get("grant"), str) else None


def usable(token: Any) -> bool:
    """Whether the stored token still stands for the person and the client (secret aside)."""
    stamp = meta(token)
    user = token.user
    return bool(
        stamp is not None
        and user is not None
        and user.is_active
        and user.deleted_at is None
        and stamp.get("auth_version") == user.auth_version
        and token.client_id
        and active_client(token.client_id) is not None
    )


def lookup(value: Any, *, types: tuple[str, ...], client_id: str | None = None) -> Any | None:
    """The live (not expired) token whose hash matches ``value``; the unique (type, hash) index serves it."""
    from allauth.idp.oidc.models import Token

    if not isinstance(value, str) or not value or len(value) > 512:
        return None
    queryset = Token.objects.valid().by_value(value).filter(type__in=types).select_related("user")
    if client_id is not None:
        queryset = queryset.filter(client_id=client_id)
    return queryset.first()


def grant_tokens(token: Any) -> Any:
    """Access and refresh tokens of the grant ``token`` belongs to (only itself when unstamped)."""
    from allauth.idp.oidc.models import Token

    stamp = meta(token)
    queryset = Token.objects.filter(type__in=(Token.Type.ACCESS_TOKEN, Token.Type.REFRESH_TOKEN))
    if stamp is None:
        return queryset.filter(pk=token.pk)
    return queryset.filter(user_id=token.user_id, client_id=token.client_id, **{f"data__{META}__grant": stamp["grant"]})


def revoke_grant(token: Any) -> int:
    deleted, _ = grant_tokens(token).delete()
    return deleted


def stamp_issued(*, access_token: str, refresh_token: str | None, previous: dict[str, Any] | None) -> None:
    """Stamp the tokens allauth just issued; a renewal keeps the grant and its start."""
    from allauth.idp.oidc.adapter import get_adapter
    from allauth.idp.oidc.models import Token

    adapter = get_adapter()
    hashes = {(Token.Type.ACCESS_TOKEN, adapter.hash_token(access_token))}
    if refresh_token:
        hashes.add((Token.Type.REFRESH_TOKEN, adapter.hash_token(refresh_token)))
    rows = [
        row
        for row in Token.objects.select_for_update(of=("self",))
        .select_related("user")
        .filter(hash__in=[h for _, h in hashes])
        if (row.type, row.hash) in hashes
    ]
    if len(rows) != len(hashes) or any(row.user is None for row in rows):
        raise RuntimeError("Issued OIDC tokens not found.")
    stamp = {
        "grant": (previous or {}).get("grant") or uuid.uuid4().hex,
        "connected_at": (previous or {}).get("connected_at") or timezone.now().isoformat(),
        "auth_version": rows[0].user.auth_version,
    }
    for row in rows:
        data = dict(row.data) if isinstance(row.data, dict) else {}
        data[META] = stamp
        row.data = data
        row.save(update_fields=["data"])


def touch(token: Any) -> None:
    """Record the last use of an access token, at most once per window (cache marker first)."""
    from allauth.idp.oidc.models import Token

    try:
        if not cache.add(f"skeleton:oidc-used:{token.pk}", 1, timeout=TOUCH_WINDOW_S):
            return
    except Exception:  # noqa: BLE001, S110 - without the cache, write anyway: one small update
        pass
    data = dict(token.data) if isinstance(token.data, dict) else {}
    stamp = dict(data.get(META) or {})
    stamp["last_used_at"] = timezone.now().isoformat()
    data[META] = stamp
    Token.objects.filter(pk=token.pk).update(data=data)


# --------------------------------------------------------------------------- connected devices


def _moment(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def connected_devices(user: Any) -> list[dict[str, Any]]:
    """One row per live grant of the person: client, connected since, last use, renewal deadline."""
    from allauth.idp.oidc.models import Token

    from skeleton.accounts.models import NativeClient

    names = dict(NativeClient.all_objects.values_list("client_id", "name"))
    grants: dict[str, dict[str, Any]] = {}
    rows = Token.objects.valid().filter(
        user_id=user.pk, type__in=(Token.Type.ACCESS_TOKEN, Token.Type.REFRESH_TOKEN), client__isnull=False
    )
    for row in rows.order_by("created_at"):
        stamp = meta(row)
        if stamp is None:
            continue
        entry = grants.setdefault(
            stamp["grant"],
            {
                "grant": stamp["grant"],
                "client_id": row.client_id,
                "client_name": names.get(row.client_id, row.client_id),
                "connected_at": _moment(stamp.get("connected_at")) or row.created_at,
                "last_used_at": row.created_at,
                "expires_at": None,
                "usable": stamp.get("auth_version") == user.auth_version,
            },
        )
        used = _moment(stamp.get("last_used_at")) or row.created_at
        entry["last_used_at"] = max(entry["last_used_at"], used)
        if row.type == Token.Type.REFRESH_TOKEN:
            entry["expires_at"] = row.expires_at
    return sorted(grants.values(), key=lambda item: item["last_used_at"], reverse=True)


def revoke_device(user: Any, grant: Any) -> int:
    """Delete the tokens of one of the person's grants; another person's grant deletes nothing."""
    from allauth.idp.oidc.models import Token

    if not isinstance(grant, str) or not re.fullmatch(r"[0-9a-f]{32}", grant):
        return 0
    deleted, _ = Token.objects.filter(user_id=user.pk, **{f"data__{META}__grant": grant}).delete()
    return deleted


def revoke_user_tokens(user: Any) -> int:
    """Every OIDC token of the person (erasure of the identity)."""
    from allauth.idp.oidc.models import Token

    deleted, _ = Token.objects.filter(user_id=user.pk).delete()
    return deleted
