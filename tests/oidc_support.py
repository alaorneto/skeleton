"""Helpers of the OIDC provider tests (#603, #604): a signing key, PKCE and the full code flow."""

from __future__ import annotations

import base64
import functools
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

from django.test import Client
from django.urls import reverse

CLIENT_ID = "acme-desktop"
REDIRECT = "http://127.0.0.1/callback"
PASSWORD = "Fixture-only-password-2026!"  # noqa: S105 - disposable password of the security fixture


@functools.cache
def rsa_pem() -> str:
    """One RSA key per test process (generation is the slow part)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def enable_provider(settings) -> None:
    settings.IDP_OIDC_PRIVATE_KEY = rsa_pem()


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(client_id: str = CLIENT_ID, *, skip_consent: bool = True, scopes=()):
    from skeleton.accounts.idp.clients import register_client
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:native-client"):
        return register_client(
            client_id=client_id, name="Acme Desktop", redirect_uris=[REDIRECT], skip_consent=skip_consent, scopes=scopes
        )


def authorize_params(challenge: str, *, port: int = 53682, **overrides) -> dict:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": f"http://127.0.0.1:{port}/callback",
        "scope": "openid profile email",
        "state": "state-1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    params.update(overrides)
    return {key: value for key, value in params.items() if value is not None}


def code_from(response) -> str | None:
    if response.status_code != 302:
        return None
    query = parse_qs(urlsplit(response["Location"]).query)
    return query.get("code", [None])[0]


def exchange(code: str, verifier: str, *, port: int = 53682, client_id: str = CLIENT_ID):
    return Client().post(
        reverse("oidc-token"),
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"http://127.0.0.1:{port}/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


def refresh(refresh_token: str, *, client_id: str = CLIENT_ID):
    return Client().post(
        reverse("oidc-token"), {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    )


def sign_in(session: Client, *, scope: str = "openid profile email") -> dict:
    """The whole flow for a signed-in browser session: the token response JSON."""
    verifier, challenge = pkce()
    response = session.get(reverse("oidc-authorize"), authorize_params(challenge, scope=scope))
    code = code_from(response)
    assert code, (response.status_code, response.get("Location"))
    tokens = exchange(code, verifier)
    assert tokens.status_code == 200, tokens.content
    return tokens.json()
