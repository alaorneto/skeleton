"""HTTP to payment providers (#400), only through ``skeleton.common.outbound_http``.

Each adapter names its base URLs as code constants (sandbox and production): nothing typed by
a person or a tenant chooses where a request goes. The response body may reach 1 MiB.
``transport`` is the single seam tests replace with recorded responses; production never
changes it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from skeleton.common import outbound_http

from .base import ProviderError

MAX_RESPONSE_BYTES = 1024 * 1024


def _default_transport(method: str, url: str, *, headers: Mapping[str, str], body: bytes | None) -> Any:
    return outbound_http.request(method, url, headers=headers, body=body, max_response_bytes=MAX_RESPONSE_BYTES)


transport = _default_transport


def call(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    json_body: Any = None,
    form: Mapping[str, Any] | list[tuple[str, Any]] | None = None,
) -> tuple[int, Any]:
    """Send one request and return ``(status, decoded JSON)``; network and 5xx are retryable errors."""
    body = None
    sent = dict(headers)
    if json_body is not None:
        body = json.dumps(json_body, separators=(",", ":")).encode()
        sent["Content-Type"] = "application/json"
    elif form is not None:
        body = urlencode(form, doseq=True).encode()
        sent["Content-Type"] = "application/x-www-form-urlencoded"
    sent.setdefault("Accept", "application/json")
    try:
        response = transport(method, url, headers=sent, body=body)
    except outbound_http.OutboundRequestRefused as exc:
        raise ProviderError(f"refused:{exc.code}") from exc
    except outbound_http.OutboundRequestFailed as exc:
        raise ProviderError("network_error", retryable=True) from exc
    if response.truncated:
        raise ProviderError("response_too_large")
    try:
        data = json.loads(response.body or b"null")
    except ValueError:
        data = None
    if response.status >= 500 or response.status == 429:
        raise ProviderError(f"http_{response.status}", retryable=True)
    return response.status, data
