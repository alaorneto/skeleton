"""SSRF-safe outbound HTTP client (#49): blocked ranges, DNS rebinding, redirects, limits."""

import ipaddress
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from skeleton.common import outbound_http
from skeleton.common.outbound_http import (
    MAX_RESPONSE_BYTES,
    OutboundRequestRefused,
    blocked_address,
    request,
    validate_url,
)

PUBLIC = "93.184.215.14"


def resolver_for(*answers):
    """A fake getaddrinfo returning a different answer on each call."""
    calls = []

    def resolve(host, port, **kwargs):
        calls.append(host)
        addresses = answers[min(len(calls), len(answers)) - 1]
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    resolve.calls = calls
    return resolve


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "169.254.169.254",
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "100.64.0.1",
        "100.127.255.254",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "192.0.2.10",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
    ],
)
def test_internal_and_reserved_addresses_are_blocked(address):
    assert blocked_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address):
    assert not blocked_address(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.1.2.3/hook",
        "https://[::1]/hook",
        "https://[::ffff:127.0.0.1]/hook",
        "https://100.64.1.1/hook",
    ],
)
def test_internal_destinations_are_refused_before_connecting(url, monkeypatch):
    def no_connection(*args, **kwargs):
        raise AssertionError("A refused destination must never be connected to.")

    monkeypatch.setattr(socket, "create_connection", no_connection)
    with pytest.raises(OutboundRequestRefused) as refused:
        request("POST", url, body=b"{}")
    assert refused.value.code == "blocked_address"


def test_a_name_with_any_internal_address_is_refused():
    with pytest.raises(OutboundRequestRefused) as refused:
        validate_url("https://mixed.example/hook", resolver=resolver_for([PUBLIC, "10.0.0.7"]))
    assert refused.value.code == "blocked_address"


@pytest.mark.parametrize(
    "url,code",
    [
        ("http://hooks.example/x", "scheme"),
        ("ftp://hooks.example/x", "scheme"),
        ("https://hooks.example:8080/x", "port"),
        ("https://user:pass@hooks.example/x", "invalid_url"),
        ("https:///x", "invalid_url"),
        ("https://hooks.example:99999/x", "invalid_url"),
    ],
)
def test_only_https_on_allowed_ports_without_credentials(url, code):
    with pytest.raises(OutboundRequestRefused) as refused:
        validate_url(url, resolver=resolver_for([PUBLIC]))
    assert refused.value.code == code


def test_allowed_ports_come_from_settings():
    with override_settings(SKELETON_OUTBOUND_HTTP_PORTS=(443, 8443)):
        assert validate_url("https://hooks.example:8443/x", resolver=resolver_for([PUBLIC])).port == 8443


def test_rebinding_resolver_cannot_move_the_connection(monkeypatch):
    """The name resolves once; a second, private answer is never used."""
    resolver = resolver_for([PUBLIC], ["127.0.0.1"])
    connected = []

    def capture(address, timeout=None, *args, **kwargs):
        connected.append((address, timeout))
        raise ConnectionRefusedError("test stops here")

    monkeypatch.setattr(socket, "create_connection", capture)
    with pytest.raises(outbound_http.OutboundRequestFailed):
        request("POST", "https://rebind.example/hook", body=b"{}", resolver=resolver)
    assert resolver.calls == ["rebind.example"]
    assert connected == [((PUBLIC, 443), outbound_http.CONNECT_TIMEOUT)]


def test_tls_uses_the_name_for_sni_and_certificate(monkeypatch):
    import ssl

    wrapped = {}

    class FakeSocket:
        def close(self):
            pass

    def fake_wrap(self, sock, server_hostname=None, **kwargs):
        wrapped["server_hostname"] = server_hostname
        raise ssl.SSLCertVerificationError("test stops here")

    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: FakeSocket())
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fake_wrap)
    with pytest.raises(outbound_http.OutboundRequestFailed):
        request("GET", "https://name.example/x", resolver=resolver_for([PUBLIC]))
    assert wrapped == {"server_hostname": "name.example"}


def test_development_override_requires_debug():
    with override_settings(DEBUG=False, SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE=True):
        with pytest.raises(ImproperlyConfigured):
            validate_url("https://hooks.example/x", resolver=resolver_for([PUBLIC]))


def _drip(handler, pieces, pause=0.2):
    try:
        for piece in pieces:
            handler.wfile.write(piece)
            handler.wfile.flush()
            time.sleep(pause)
    except OSError:
        pass  # The client gave up: expected.


class _Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.received.append((self.path, self.headers.get("Host"), self.rfile.read(length)))
        if self.path == "/drip-body":
            self.send_response(200)
            self.send_header("Content-Length", "200")
            self.end_headers()
            _drip(self, [b"x"] * 200)
            return
        if self.path == "/drip-headers":
            self.wfile.write(b"HTTP/1.1 200 OK\r\n")
            _drip(self, [b"X-Filler: 1\r\n"] * 200)
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            return
        payload = b"x" * (MAX_RESPONSE_BYTES * 2) if self.path == "/large" else b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def receiver():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_local_receiver_only_with_the_development_override(receiver):
    url = f"http://127.0.0.1:{receiver.server_port}/hook"
    with pytest.raises(OutboundRequestRefused):
        request("POST", url, body=b"{}")
    assert receiver.received == []


def test_redirects_are_not_followed_bodies_are_truncated_and_proxies_ignored(receiver, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    base = f"http://127.0.0.1:{receiver.server_port}"
    with override_settings(DEBUG=True, SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE=True):
        moved = request("POST", base + "/redirect", body=b"{}")
        assert moved.status == 302 and moved.headers["location"] == "http://127.0.0.1:1/elsewhere"
        large = request("POST", base + "/large", body=b"{}")
        assert large.status == 200 and large.truncated and len(large.body) == MAX_RESPONSE_BYTES
        plain = request("POST", base + "/hook?x=1", body=b'{"a":1}', headers={"Content-Type": "application/json"})
        assert plain.status == 200 and plain.body == b"ok" and not plain.truncated
    # Exactly the three requests arrived, straight to the receiver (no proxy, no redirect follow-up).
    assert [path for path, _, _ in receiver.received] == ["/redirect", "/large", "/hook?x=1"]
    assert receiver.received[-1][1] == f"127.0.0.1:{receiver.server_port}"
    assert receiver.received[-1][2] == b'{"a":1}'


@pytest.mark.parametrize("path", ["/drip-body", "/drip-headers"])
def test_total_deadline_bounds_a_dripping_answer(receiver, monkeypatch, path):
    """Each read is fast, the whole answer is not: the total deadline still ends it (#224)."""
    monkeypatch.setattr(outbound_http, "TOTAL_TIMEOUT", 1.0)
    base = f"http://127.0.0.1:{receiver.server_port}"
    started = time.monotonic()
    with override_settings(DEBUG=True, SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE=True):
        with pytest.raises(outbound_http.OutboundRequestFailed):
            request("POST", base + path, body=b"{}")
    assert time.monotonic() - started < 5


def test_name_resolution_has_its_own_timeout():
    def slow(*args, **kwargs):
        time.sleep(3)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 443))]

    started = time.monotonic()
    with pytest.raises(OutboundRequestRefused) as refused:
        validate_url("https://slow.example/x", resolver=slow, timeout=0.3)
    assert refused.value.code == "unresolvable" and time.monotonic() - started < 2


def test_deadline_timer_is_cancelled_after_the_request(receiver):
    base = f"http://127.0.0.1:{receiver.server_port}"
    with override_settings(DEBUG=True, SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE=True):
        assert request("POST", base + "/hook", body=b"{}").status == 200
    timers = [thread for thread in threading.enumerate() if isinstance(thread, threading.Timer)]
    for timer in timers:
        timer.join(2)  # A cancelled timer thread ends on its own, a moment later.
    assert not any(timer.is_alive() for timer in timers)


def test_tls_context_is_built_once():
    assert outbound_http._tls_context() is outbound_http._tls_context()
