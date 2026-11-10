"""The only way the core makes HTTP requests to addresses chosen by third parties (#49).

Webhook and push endpoints are URLs typed by tenants, so a naive client is an SSRF primitive:
it could reach the internal network, the cloud metadata service or the database. This client:

- accepts only ``https`` on allowed ports (``SKELETON_OUTBOUND_HTTP_PORTS``, default 443) and
  refuses credentials in the URL;
- resolves the name **once**, refuses the host if **any** address is not public (private,
  loopback, link-local, CGNAT, reserved, multicast, unspecified, IPv4-mapped/translated forms);
- connects to the address it validated, sending SNI and ``Host`` with the name, so a resolver
  that answers differently a second time (DNS rebinding) is never consulted again;
- never follows redirects and ignores proxy environment variables (``http.client`` reads
  none); connects in 5 s, reads in 10 s and keeps at most 64 KB of the response body.

``SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE`` lets a development instance (``DEBUG=True``)
call ``http://`` and private addresses, such as a local receiver; with ``DEBUG=False`` it is
refused at the first request.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_PORTS = (443,)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[..., Sequence[tuple[Any, ...]]]


class OutboundRequestRefused(Exception):
    """The destination is not allowed; ``code`` says why (never retry the same URL)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class OutboundRequestFailed(Exception):
    """The allowed destination did not answer in time or broke the connection (retryable)."""


@dataclass(frozen=True)
class OutboundResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    truncated: bool


def blocked_address(address: Address) -> bool:
    """True for every address that is not a public unicast destination."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return blocked_address(address.ipv4_mapped)
        if address in _NAT64 or address.sixtofour is not None or address.teredo is not None:
            return True
    if isinstance(address, ipaddress.IPv4Address) and address in _CGNAT:
        return True
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )


def _settings() -> tuple[tuple[int, ...], bool]:
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    ports = tuple(getattr(settings, "SKELETON_OUTBOUND_HTTP_PORTS", DEFAULT_PORTS))
    override = bool(getattr(settings, "SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE", False))
    if override and not settings.DEBUG:
        raise ImproperlyConfigured("SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE is only for DEBUG instances.")
    return ports, override


def _addresses(host: str, port: int, resolver: Resolver) -> list[Address]:
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        return [literal]
    try:
        answers = resolver(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        raise OutboundRequestRefused("unresolvable", "O nome do destino não resolve.") from exc
    addresses = list(dict.fromkeys(ipaddress.ip_address(answer[4][0].split("%")[0]) for answer in answers))
    if not addresses:
        raise OutboundRequestRefused("unresolvable", "O nome do destino não resolve.")
    return addresses


@dataclass(frozen=True)
class Destination:
    scheme: str
    host: str
    port: int
    target: str  # path and query sent on the request line
    address: Address  # the validated address the connection goes to


def validate_url(url: str, *, resolver: Resolver | None = None) -> Destination:
    """Parse, resolve and validate a URL; refuses before any connection is attempted."""
    ports, override = _settings()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise OutboundRequestRefused("invalid_url", "URL inválida.") from exc
    allowed_schemes = {"https", "http"} if override else {"https"}
    if parts.scheme not in allowed_schemes:
        raise OutboundRequestRefused("scheme", "Somente HTTPS é permitido.")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise OutboundRequestRefused("invalid_url", "A URL precisa de um host e não pode ter credenciais.")
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in ports and not override:
        raise OutboundRequestRefused("port", "Porta de destino não permitida.")
    # Looked up at call time, so tests and callers may replace the module-level resolver.
    addresses = _addresses(parts.hostname, port, resolver or socket.getaddrinfo)
    if not override and any(blocked_address(address) for address in addresses):
        raise OutboundRequestRefused("blocked_address", "O destino aponta para um endereço interno ou reservado.")
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return Destination(parts.scheme, parts.hostname, port, target, addresses[0])


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a pre-validated address; SNI, certificate name and ``Host`` stay the URL's name."""

    def __init__(self, destination: Destination, context: ssl.SSLContext):
        super().__init__(destination.host, destination.port, timeout=CONNECT_TIMEOUT, context=context)
        self._destination = destination
        self._context = context

    def connect(self) -> None:
        raw = socket.create_connection((str(self._destination.address), self.port), timeout=CONNECT_TIMEOUT)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
        self.sock.settimeout(READ_TIMEOUT)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Development override only: plain HTTP to the validated address."""

    def __init__(self, destination: Destination):
        super().__init__(destination.host, destination.port, timeout=CONNECT_TIMEOUT)
        self._destination = destination

    def connect(self) -> None:
        self.sock = socket.create_connection((str(self._destination.address), self.port), timeout=CONNECT_TIMEOUT)
        self.sock.settimeout(READ_TIMEOUT)


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    resolver: Resolver | None = None,
) -> OutboundResponse:
    """Send one request to a validated destination; redirects come back as plain 3xx responses."""
    destination = validate_url(url, resolver=resolver)
    if destination.scheme == "https":
        connection: http.client.HTTPConnection = _PinnedHTTPSConnection(destination, ssl.create_default_context())
    else:
        connection = _PinnedHTTPConnection(destination)
    try:
        connection.request(method, destination.target, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        return OutboundResponse(
            status=response.status,
            headers={name.lower(): value for name, value in response.getheaders()},
            body=data[:MAX_RESPONSE_BYTES],
            truncated=len(data) > MAX_RESPONSE_BYTES,
        )
    except (OSError, http.client.HTTPException) as exc:
        raise OutboundRequestFailed(f"Falha de rede com o destino: {type(exc).__name__}.") from exc
    finally:
        connection.close()
