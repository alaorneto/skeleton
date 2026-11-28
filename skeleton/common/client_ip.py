"""Client IP resolution under the configured proxy topology (#862).

Relies on Django's/allauth's configured trusted proxy settings (``ALLAUTH_TRUSTED_PROXY_COUNT``
and ``ALLAUTH_TRUSTED_CLIENT_IP_HEADER``, derived from ``DJANGO_TRUST_PROXY`` in
``skeleton.api.proxy``): when no trusted proxy is configured, forwarded headers are ignored and
the peer ``REMOTE_ADDR`` is returned.
"""

from __future__ import annotations

from typing import Any


def client_ip(request: Any) -> str:
    """Return the client address under the configured proxy topology, falling back to ``REMOTE_ADDR``."""
    try:
        from allauth.account.adapter import get_adapter

        return str(get_adapter(request).get_client_ip(request))
    except Exception:  # noqa: BLE001 - a malformed forwarded header falls back to the peer
        return str(request.META.get("REMOTE_ADDR") or "unknown")
