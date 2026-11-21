"""allauth OIDC adapter of the core (#603): canonical issuer, live identities and honest discovery."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from allauth.idp.oidc.adapter import DefaultOIDCAdapter

from .clients import GRANT_TYPES, OIDC_SCOPES, canonical_origin

_ENDPOINTS = (
    "authorization_endpoint",
    "end_session_endpoint",
    "jwks_uri",
    "revocation_endpoint",
    "token_endpoint",
    "userinfo_endpoint",
)


class SkeletonOIDCAdapter(DefaultOIDCAdapter):
    def get_issuer(self) -> str:
        return canonical_origin(self.request)

    def get_user_by_sub(self, client: Any, sub: str) -> Any:
        user = super().get_user_by_sub(client, sub)
        if user is None or user.deleted_at is not None:
            return None
        return user

    def populate_server_metadata(self, data: dict[str, Any]) -> None:
        """Publish only what a native client may use, on the canonical origin."""
        origin = urlsplit(canonical_origin(self.request))
        for name in _ENDPOINTS:
            value = data.get(name)
            if isinstance(value, str):
                parts = urlsplit(value)
                data[name] = urlunsplit((origin.scheme, origin.netloc, parts.path, parts.query, ""))
        data.pop("device_authorization_endpoint", None)
        data["grant_types_supported"] = list(GRANT_TYPES)
        data["response_types_supported"] = ["code"]
        data["code_challenge_methods_supported"] = ["S256"]
        data["token_endpoint_auth_methods_supported"] = ["none"]
        data["scopes_supported"] = list(OIDC_SCOPES)
