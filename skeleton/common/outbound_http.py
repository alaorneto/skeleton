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
  none); resolves the name in 5 s, connects in 5 s and reads in 10 s per operation, ends the
  whole exchange within 20 s however slowly the destination answers (#224), and keeps at most
  64 KB of the response body.

``SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE`` lets a development instance (``DEBUG=True``)
call ``http://`` and private addresses, such as a local receiver; with ``DEBUG=False`` it is
refused at the first request.
"""

from __future__ import annotations

import functools
import http.client
import ipaddress
import socket
import ssl
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from django.utils.translation import gettext

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
DNS_TIMEOUT = 5.0
TOTAL_TIMEOUT = 20.0
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


@functools.lru_cache(maxsize=1)
def _tls_context() -> ssl.SSLContext:
    """The default client context (system CAs, hostname check), built once per process."""
    return ssl.create_default_context()


class _AbortableConnection:
    """Lets the deadline timer end a request that is stuck in the handshake, a send or a read.

    ``abort()`` runs in the timer thread. It only shuts the transport down, which makes the
    blocked call in the request thread fail or read end of stream; the request thread closes the
    socket (closing it here could hand its file descriptor to another connection while the
    request thread still uses it).
    """

    _abortable: Any = None
    aborted = False

    def abort(self) -> None:
        self.aborted = True
        sock = self._abortable
        if sock is not None:
            try:
                # The base-class call leaves an ``SSLSocket`` wrapped: only the transport goes down.
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except (OSError, TypeError, ValueError):
                pass


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


def _resolve(host: str, port: int, resolver: Resolver, timeout: float) -> Sequence[tuple[Any, ...]]:
    """Run the resolver in a daemon thread: ``getaddrinfo`` has no timeout of its own (#224)."""
    outcome: list[tuple[str, Any]] = []

    def work() -> None:
        try:
            outcome.append(("ok", resolver(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)))
        except (OSError, UnicodeError) as exc:
            outcome.append(("error", exc))

    thread = threading.Thread(target=work, name="outbound-dns", daemon=True)
    thread.start()
    thread.join(timeout)
    if not outcome:
        raise OutboundRequestRefused("unresolvable", gettext("O nome do destino não resolveu a tempo."))
    kind, value = outcome[0]
    if kind == "error":
        raise OutboundRequestRefused("unresolvable", gettext("O nome do destino não resolve.")) from value
    answers: Sequence[tuple[Any, ...]] = value
    return answers


def _addresses(host: str, port: int, resolver: Resolver, timeout: float = DNS_TIMEOUT) -> list[Address]:
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        return [literal]
    answers = _resolve(host, port, resolver, timeout)
    addresses = list(dict.fromkeys(ipaddress.ip_address(answer[4][0].split("%")[0]) for answer in answers))
    if not addresses:
        raise OutboundRequestRefused("unresolvable", gettext("O nome do destino não resolve."))
    return addresses


@dataclass(frozen=True)
class Destination:
    scheme: str
    host: str
    port: int
    target: str  # path and query sent on the request line
    address: Address  # the validated address the connection goes to


def validate_url(url: str, *, resolver: Resolver | None = None, timeout: float = DNS_TIMEOUT) -> Destination:
    """Parse, resolve and validate a URL; refuses before any connection is attempted."""
    ports, override = _settings()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise OutboundRequestRefused("invalid_url", gettext("URL inválida.")) from exc
    allowed_schemes = {"https", "http"} if override else {"https"}
    if parts.scheme not in allowed_schemes:
        raise OutboundRequestRefused("scheme", gettext("Somente HTTPS é permitido."))
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise OutboundRequestRefused("invalid_url", gettext("A URL precisa de um host e não pode ter credenciais."))
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in ports and not override:
        raise OutboundRequestRefused("port", gettext("Porta de destino não permitida."))
    # Looked up at call time, so tests and callers may replace the module-level resolver.
    addresses = _addresses(parts.hostname, port, resolver or socket.getaddrinfo, timeout)
    if not override and any(blocked_address(address) for address in addresses):
        raise OutboundRequestRefused(
            "blocked_address", gettext("O destino aponta para um endereço interno ou reservado.")
        )
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return Destination(parts.scheme, parts.hostname, port, target, addresses[0])


class _PinnedHTTPSConnection(_AbortableConnection, http.client.HTTPSConnection):
    """HTTPS to a pre-validated address; SNI, certificate name and ``Host`` stay the URL's name."""

    def __init__(self, destination: Destination, context: ssl.SSLContext):
        super().__init__(destination.host, destination.port, timeout=CONNECT_TIMEOUT, context=context)
        self._destination = destination
        self._context = context

    def connect(self) -> None:
        raw = socket.create_connection((str(self._destination.address), self.port), timeout=CONNECT_TIMEOUT)
        try:
            # The handshake is a separate step so the deadline timer can reach the socket during it.
            secured = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self._abortable = secured
            if self.aborted:
                raise OutboundRequestFailed(gettext("Prazo total da requisição excedido."))
            secured.do_handshake()
        except BaseException:
            raw.close()
            if self._abortable is not None:
                self._abortable.close()
            raise
        self.sock = secured
        self.sock.settimeout(READ_TIMEOUT)


class _PinnedHTTPConnection(_AbortableConnection, http.client.HTTPConnection):
    """Development override only: plain HTTP to the validated address."""

    def __init__(self, destination: Destination):
        super().__init__(destination.host, destination.port, timeout=CONNECT_TIMEOUT)
        self._destination = destination

    def connect(self) -> None:
        self.sock = socket.create_connection((str(self._destination.address), self.port), timeout=CONNECT_TIMEOUT)
        self._abortable = self.sock
        self.sock.settimeout(READ_TIMEOUT)


def request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    resolver: Resolver | None = None,
) -> OutboundResponse:
    """Send one request to a validated destination; redirects come back as plain 3xx responses.

    The exchange after name resolution (connection, TLS handshake, request, response headers and
    body) ends within ``TOTAL_TIMEOUT`` seconds, however slowly the destination answers; past it
    the request fails with ``OutboundRequestFailed``. Name resolution has ``DNS_TIMEOUT`` of its own.
    """
    destination = validate_url(url, resolver=resolver)
    connection: _PinnedHTTPSConnection | _PinnedHTTPConnection
    if destination.scheme == "https":
        connection = _PinnedHTTPSConnection(destination, _tls_context())
    else:
        connection = _PinnedHTTPConnection(destination)
    # ``CONNECT_TIMEOUT``/``READ_TIMEOUT`` bound each socket operation; this timer bounds their sum.
    timer = threading.Timer(TOTAL_TIMEOUT, connection.abort)
    timer.daemon = True
    timer.start()
    try:
        connection.request(method, destination.target, body=body, headers=dict(headers or {}))
        response = connection.getresponse()
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if connection.aborted:
            # A shut down socket reads as end of stream: never report a cut answer as complete.
            raise OutboundRequestFailed(gettext("Prazo total da requisição excedido."))
        return OutboundResponse(
            status=response.status,
            headers={name.lower(): value for name, value in response.getheaders()},
            body=data[:MAX_RESPONSE_BYTES],
            truncated=len(data) > MAX_RESPONSE_BYTES,
        )
    except (OSError, http.client.HTTPException) as exc:
        if connection.aborted:
            raise OutboundRequestFailed(gettext("Prazo total da requisição excedido.")) from exc
        raise OutboundRequestFailed(
            gettext("Falha de rede com o destino: %(__name__)s.") % {"__name__": type(exc).__name__}
        ) from exc
    finally:
        timer.cancel()
        connection.close()
