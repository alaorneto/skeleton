"""The HTTP client of the platform API of a remote instance (#712), over ``outbound_http``.

Only ``https`` (and the ports ``outbound_http`` allows), no redirect followed, answers up to
1 MiB. The token is revealed only for the header of each request. A status other than 200 and
a body that is not a JSON object become ``RemoteInstanceError`` with a code; the body of the
remote is never echoed. Tests replace ``transport`` (``skeleton.testing.remote_instances``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

MAX_BYTES = 1024 * 1024
API_PREFIX = "/api/v1/platform/"


class RemoteInstanceError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _default_transport(url: str, headers: Mapping[str, str]) -> tuple[int, bytes, bool]:
    from skeleton.common.outbound_http import OutboundRequestFailed, OutboundRequestRefused, request

    try:
        response = request("GET", url, headers=headers, max_response_bytes=MAX_BYTES)
    except (OutboundRequestFailed, OutboundRequestRefused):
        raise RemoteInstanceError("remote_unavailable") from None
    return response.status, response.body, response.truncated


transport: Callable[[str, Mapping[str, str]], tuple[int, bytes, bool]] = _default_transport


def validate_base_url(value: Any) -> str:
    """``https://host[:port]`` with no path (``/`` at most), no credential, query or fragment."""
    if not isinstance(value, str):
        raise ValidationError({"base_url": gettext("Use um endereço https://.")})
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValidationError({"base_url": gettext("Use um endereço https://.")})
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValidationError({"base_url": gettext("Informe só o endereço base, sem caminho ou credencial.")})
    return value


_STATUS = {401: "remote_unauthorized", 403: "remote_forbidden", 404: "remote_not_found"}


def get(connection: Any, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """GET ``/api/v1/platform/<path>`` of the remote; a JSON object or ``RemoteInstanceError``."""
    query = urlencode([(key, value) for key, value in (params or {}).items() if value is not None], doseq=True)
    url = connection.base_url.rstrip("/") + API_PREFIX + path.lstrip("/") + (f"?{query}" if query else "")
    token = connection.token.reveal() if hasattr(connection.token, "reveal") else str(connection.token)
    status, body, truncated = transport(url, {"Authorization": f"Bearer {token}", "Accept": "application/json"})
    if status != 200:
        raise RemoteInstanceError(_STATUS.get(status, "remote_unavailable"))
    if truncated:
        raise RemoteInstanceError("remote_invalid_response")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RemoteInstanceError("remote_invalid_response") from None
    if not isinstance(data, dict):
        raise RemoteInstanceError("remote_invalid_response")
    if path.strip("/") == "instance" and connection.expected_instance_id is not None:
        if str(data.get("instance_id")) != str(connection.expected_instance_id):
            raise RemoteInstanceError("remote_instance_mismatch")
    return data
