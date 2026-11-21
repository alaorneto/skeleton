"""Signing keys of the OIDC provider for native clients (#603), read once when the settings load.

The private keys never live in the repository: they come from the environment
(``IDP_OIDC_PRIVATE_KEY``, a PEM) or from files the deployment provisions with mode 0600
(``IDP_OIDC_PRIVATE_KEY_FILE``, a PEM; ``IDP_OIDC_PRIVATE_KEYS_FILE``, a JSON list of
``{"pem", "not_before", "expires_at", "issued_at"}`` for a rotation). Without any key the provider
stays off: its routes answer 404 and ``/api/v1/server/`` reports no issuer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.exceptions import ImproperlyConfigured


def _read(path: str, name: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ImproperlyConfigured(f"{name}: the key file cannot be read.") from exc


def oidc_private_keys(env: Any) -> tuple[str, list[dict[str, Any]]]:
    """``(IDP_OIDC_PRIVATE_KEY, IDP_OIDC_PRIVATE_KEYS)`` from the environment and the key files."""
    single = env("IDP_OIDC_PRIVATE_KEY", default="") or ""
    single_file = env("IDP_OIDC_PRIVATE_KEY_FILE", default="") or ""
    if single and single_file:
        raise ImproperlyConfigured("Use IDP_OIDC_PRIVATE_KEY or IDP_OIDC_PRIVATE_KEY_FILE, not both.")
    if single_file:
        single = _read(single_file, "IDP_OIDC_PRIVATE_KEY_FILE")
    keys: list[dict[str, Any]] = []
    keys_file = env("IDP_OIDC_PRIVATE_KEYS_FILE", default="") or ""
    if keys_file:
        try:
            keys = json.loads(_read(keys_file, "IDP_OIDC_PRIVATE_KEYS_FILE"))
        except ValueError as exc:
            raise ImproperlyConfigured("IDP_OIDC_PRIVATE_KEYS_FILE must hold a JSON list.") from exc
        if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
            raise ImproperlyConfigured("IDP_OIDC_PRIVATE_KEYS_FILE must hold a JSON list of objects.")
    for pem in [single, *(key.get("pem") for key in keys)]:
        if pem and (not isinstance(pem, str) or "PRIVATE KEY-----" not in pem):
            raise ImproperlyConfigured("The OIDC provider keys must be PEM RSA private keys.")
    return single.strip() + "\n" if single.strip() else "", keys
