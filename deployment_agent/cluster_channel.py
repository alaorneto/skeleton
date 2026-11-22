"""The channel between the agents of a cluster (#680): artifacts by digest and forwarded requests.

The web of every node keeps talking only to its own agent's Unix socket. Between agents there is
one HTTPS listener per node (``cluster.listen``, a private address), with mutual TLS against the
cluster's private CA (``tls_ca``; each node has ``tls_cert`` and ``tls_key``) **and** an HMAC on
every request with the cluster secret (``secret_file``, 32 bytes or more, the same on every node):
the header ``X-Skeleton-Cluster: <node_id>;<epoch>;<timestamp>;<hex>`` signs the method, the path,
the time, the node, the epoch and the SHA-256 of the body. A wrong signature, a clock more than
``MAX_SKEW_S`` away or a node the coordination does not know answers the same 401.

Routes, only these:

- ``GET /v1/health``: who answers and whether it is the primary.
- ``GET /v1/artifacts/<sha256>`` (primary): a package referenced by a plan or by the active state.
  The follower checks the SHA-256 and then the publisher's signature before using it, so the
  channel never decides what code runs.
- ``POST /v1/uploads`` (primary): a package a follower's web uploaded.
- ``POST /v1/forward`` (primary): ``{"method", "path", "body"}`` of a request a follower's web made
  to its socket; the primary runs it through the same routes as its own socket.

No route of recovery, no command, no secret; nothing of this reaches the public proxy.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .storage import AgentError, log_internal_error
from .types import JSON

if TYPE_CHECKING:
    from .service import DeploymentService

HEADER = "X-Skeleton-Cluster"
MAX_SKEW_S = 60
SECRET_MIN_BYTES = 32
DIGEST = re.compile(r"[0-9a-f]{64}")
# Requests a follower's socket answers itself; everything else goes to the primary.
LOCAL_GETS = frozenset({"/inventory", "/cluster"})
FORWARD_TIMEOUT_S = 30
UPLOAD_TIMEOUT_S = 600
MAX_CONCURRENT_FORWARDS = 4
CHANNEL_KEYS = ("listen", "tls_cert", "tls_key", "tls_ca", "secret_file")


def validate_channel(cluster: JSON) -> None:
    """Shape of the channel keys of ``cluster`` (#680); the files are read when the listener starts."""
    listen = cluster.get("listen")
    if not isinstance(listen, str) or not re.fullmatch(r"[0-9A-Za-z.:\[\]-]+:[0-9]{1,5}", listen):
        raise AgentError("invalid_cluster_channel")
    for key in CHANNEL_KEYS[1:]:
        if not isinstance(cluster.get(key), str) or not Path(cluster[key]).is_absolute():
            raise AgentError("invalid_cluster_channel")
    if not cluster.get("cluster_url"):
        raise AgentError("invalid_cluster_channel")


def read_secret(path: str) -> bytes:
    from .storage import private_file

    try:
        secret = private_file(path).read_bytes().strip()
    except (OSError, ValueError):
        raise AgentError("invalid_cluster_channel") from None
    if len(secret) < SECRET_MIN_BYTES:
        raise AgentError("invalid_cluster_channel")
    return secret


def write_secret(path: str) -> JSON:
    """``cluster keygen``: a new random secret, 0600, never over an existing file."""
    import secrets

    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise AgentError("cluster_secret_exists", 409) from None
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(secrets.token_hex(32) + "\n")
    return {"state": "written", "path": path}


def signature(secret: bytes, *, method: str, path: str, timestamp: int, node_id: str, epoch: int, body: bytes) -> str:
    message = "\n".join(
        (method.upper(), path, str(timestamp), node_id, str(epoch), hashlib.sha256(body).hexdigest())
    ).encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def sign(
    secret: bytes, *, method: str, path: str, node_id: str, epoch: int, body: bytes, now: float | None = None
) -> str:
    timestamp = int(time.time() if now is None else now)
    digest = signature(secret, method=method, path=path, timestamp=timestamp, node_id=node_id, epoch=epoch, body=body)
    return f"{node_id};{epoch};{timestamp};{digest}"


def verify(
    secret: bytes, header: str, *, method: str, path: str, body: bytes, now: float | None = None
) -> tuple[str, int]:
    """``(node_id, epoch)`` of a valid signature; anything else is the same 401."""
    parts = header.split(";")
    if len(parts) != 4 or not parts[1].lstrip("-").isdigit() or not parts[2].isdigit():
        raise AgentError("unauthorized", 401)
    node_id, epoch, timestamp, supplied = parts[0], int(parts[1]), int(parts[2]), parts[3]
    current = time.time() if now is None else now
    if abs(current - timestamp) > MAX_SKEW_S:
        raise AgentError("unauthorized", 401)
    expected = signature(secret, method=method, path=path, timestamp=timestamp, node_id=node_id, epoch=epoch, body=body)
    if not hmac.compare_digest(expected.encode(), supplied.encode("utf-8", "replace")):
        raise AgentError("unauthorized", 401)
    return node_id, epoch


def server_context(cluster: JSON) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cluster["tls_cert"], cluster["tls_key"])
    context.load_verify_locations(cluster["tls_ca"])
    context.verify_mode = ssl.CERT_REQUIRED  # Mutual TLS: the client presents a certificate of the CA.
    return context


def client_context(cluster: JSON) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cluster["tls_ca"])
    context.load_cert_chain(cluster["tls_cert"], cluster["tls_key"])
    return context


def allowed_artifacts(service: Any) -> set[str]:
    """Digests a follower may fetch: referenced by a plan or by the active state."""
    allowed: set[str] = set()
    for _, record, _ in service.store.plans():
        plan = record.get("plan") or {}
        allowed.update(item for item in plan.get("artifacts", []) if isinstance(item, str))
        allowed.update(item for item in record.get("artifacts", []) if isinstance(item, str))
    try:
        inventory = service.state()["inventory"]
    except AgentError:
        inventory = {}
    for entry in (inventory.get("core") or {}, *(inventory.get("apps") or [])):
        for key in ("digest", "release_package"):
            if isinstance(entry.get(key), str):
                allowed.add(entry[key])
    return {item for item in allowed if DIGEST.fullmatch(item)}


class Routes:
    """The routes of the control socket, run for a forwarded request (no socket, no token)."""

    def __init__(self, service: Any, tickets: Any) -> None:
        self.server = SimpleNamespace(service=service, tickets=tickets)
        self.restart_after_response = False

    def call(self, method: str, path: str, body: Any) -> Any:
        from .server import ControlHandler

        if urlsplit(path).query or urlsplit(path).fragment:
            raise AgentError("invalid_control_path")
        if method == "GET":
            return ControlHandler._get(self, path)  # type: ignore[arg-type]
        if method == "POST":
            if not isinstance(body, dict):
                raise AgentError("invalid_json")
            route = ControlHandler.POST_ROUTES.get(path)
            if route is not None:
                return route(self, body)  # type: ignore[arg-type]
        raise AgentError("not_found", 404)

    def operation(self, operation_id: str) -> JSON:
        from .server import ControlHandler

        return ControlHandler.operation(self, operation_id)  # type: ignore[arg-type]


class ChannelHandler(BaseHTTPRequestHandler):
    server: Any

    def log_message(self, format: str, *arguments: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler's name
        return

    def _send(self, status: int, data: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, value: Any, status: int = 200) -> None:
        self._send(status, json.dumps(value, sort_keys=True).encode())

    def _body(self, maximum: int) -> bytes:
        try:
            size = int(self.headers["Content-Length"] or 0)
        except (TypeError, ValueError):
            raise AgentError("content_length_required", 411) from None
        if size < 0 or size > maximum:
            raise AgentError("request_too_large", 413)
        data = self.rfile.read(size)
        if len(data) != size:
            raise AgentError("incomplete_request")
        return data

    def _authenticated(self, body: bytes) -> tuple[str, int]:
        channel: ClusterChannel = self.server.channel
        node_id, epoch = verify(
            channel.secret, self.headers.get(HEADER, ""), method=self.command, path=self.path, body=body
        )
        if not channel.known_node(node_id):
            raise AgentError("unauthorized", 401)
        return node_id, epoch

    def _primary(self) -> None:
        coordinator = self.server.channel.service.coordinator
        if coordinator is None or not coordinator.is_primary:
            raise AgentError("not_primary", 409)

    def handle_request(self) -> None:
        channel: ClusterChannel = self.server.channel
        service = channel.service
        try:
            maximum = service.config.get("max_upload_bytes", 128 * 1024 * 1024)
            body = self._body(maximum if self.path == "/v1/uploads" else 1024 * 1024)
            node_id, _ = self._authenticated(body)
            if self.command == "GET" and self.path == "/v1/health":
                coordinator = service.coordinator
                self._json({"node_id": channel.node_id, "is_primary": bool(coordinator and coordinator.is_primary)})
                return
            self._primary()
            if self.command == "GET" and self.path.startswith("/v1/artifacts/"):
                digest = self.path.removeprefix("/v1/artifacts/")
                path = service.store.path("uploads", digest) if DIGEST.fullmatch(digest) else None
                if path is None or digest not in allowed_artifacts(service) or not path.is_file():
                    raise AgentError("not_found", 404)
                self._send(200, path.read_bytes(), "application/octet-stream")
                return
            if self.command == "POST" and self.path == "/v1/uploads":
                self._json(service.upload(body))
                return
            if self.command == "POST" and self.path == "/v1/forward":
                try:
                    request = json.loads(body)
                except ValueError:
                    raise AgentError("invalid_json") from None
                if not isinstance(request, dict) or set(request) - {"method", "path", "body"}:
                    raise AgentError("invalid_forward")
                method, path = request.get("method"), request.get("path")
                if method not in ("GET", "POST") or not isinstance(path, str) or not path.startswith("/"):
                    raise AgentError("invalid_forward")
                routes = Routes(service, channel.tickets)
                result = routes.call(method, path, request.get("body"))
                if isinstance(result, dict) and result.get("id") and method == "POST" and path == "/submit":
                    result.setdefault("source_node", node_id)
                self._json(result)
                return
            raise AgentError("not_found", 404)
        except AgentError as error:
            payload: JSON = {"error": error.code}
            if error.details:
                payload["details"] = error.details
            self._json(payload, error.status)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as error:  # noqa: BLE001 - unexpected errors become a 500 with class and stack only
            log_internal_error(error)
            self._json({"error": "agent_internal_error"}, 500)
        finally:
            self.close_connection = True

    do_GET = handle_request
    do_POST = handle_request


class ClusterChannel:
    """One node's listener and client of the channel."""

    def __init__(self, service: DeploymentService, tickets: Any = None) -> None:
        cluster = service.config["cluster"]
        self.service = service
        self.tickets = tickets
        self.cluster = cluster
        self.node_id = cluster["node_id"]
        self.secret = read_secret(cluster["secret_file"])
        self._known: tuple[float, set[str]] = (0.0, set())
        self._forwards = threading.BoundedSemaphore(MAX_CONCURRENT_FORWARDS)
        self.server: ThreadingHTTPServer | None = None

    # ------------------------------------------------------------------ listener

    def known_node(self, node_id: str) -> bool:
        stamp, known = self._known
        if time.monotonic() - stamp > 10:
            coordinator = self.service.coordinator
            known = {node["node_id"] for node in coordinator.nodes()} if coordinator else set()
            self._known = (time.monotonic(), known)
        return node_id in known

    def start(self) -> None:
        host, _, port = self.cluster["listen"].rpartition(":")
        server = ThreadingHTTPServer((host.strip("[]"), int(port)), ChannelHandler)
        server.socket = server_context(self.cluster).wrap_socket(server.socket, server_side=True)
        server.channel = self  # type: ignore[attr-defined]
        self.server = server
        threading.Thread(target=server.serve_forever, daemon=True, name="deployment-cluster-channel").start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    # ------------------------------------------------------------------ client

    def _primary_url(self) -> str:
        coordinator = self.service.coordinator
        primary = coordinator.primary() if coordinator else None
        if not primary:
            raise AgentError("no_primary", 503)
        if not primary["cluster_url"]:
            raise AgentError("primary_unreachable", 503)
        return str(primary["cluster_url"])

    def request(
        self, method: str, path: str, body: bytes = b"", *, url: str | None = None, timeout: float = FORWARD_TIMEOUT_S
    ) -> tuple[int, bytes]:
        target = urlsplit(url or self._primary_url())
        coordinator = self.service.coordinator
        epoch = (coordinator.epoch if coordinator and coordinator.epoch is not None else 0) if coordinator else 0
        headers = {
            HEADER: sign(self.secret, method=method, path=path, node_id=self.node_id, epoch=epoch, body=body),
            "Content-Length": str(len(body)),
            "Content-Type": "application/octet-stream" if path == "/v1/uploads" else "application/json",
        }
        connection = http.client.HTTPSConnection(
            target.hostname or "", target.port or 443, context=client_context(self.cluster), timeout=timeout
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(UPLOAD_LIMIT)
        except (OSError, http.client.HTTPException):
            raise AgentError("primary_unreachable", 503) from None
        finally:
            connection.close()

    def _json_result(self, status: int, data: bytes) -> Any:
        try:
            value = json.loads(data)
        except ValueError:
            raise AgentError("primary_unreachable", 503) from None
        if status >= 400:
            code = value.get("error") if isinstance(value, dict) else None
            raise AgentError(code if isinstance(code, str) else "primary_unreachable", status, value.get("details"))
        return value

    def forward(self, method: str, path: str, body: Any) -> Any:
        """A request the web made to this follower's socket, run by the primary."""
        if not self._forwards.acquire(blocking=False):
            raise AgentError("too_many_forwarded_requests", 503)
        try:
            if method == "POST" and path == "/uploads":
                status, data = self.request("POST", "/v1/uploads", body, timeout=UPLOAD_TIMEOUT_S)
            else:
                envelope = json.dumps({"method": method, "path": path, "body": body}).encode()
                status, data = self.request("POST", "/v1/forward", envelope)
            return self._json_result(status, data)
        finally:
            self._forwards.release()

    def fetch_artifact(self, digest: str) -> Path:
        """Download a package of the primary into this node's uploads/, checked by its SHA-256."""
        from .storage import atomic_bytes

        if not DIGEST.fullmatch(digest):
            raise AgentError("invalid_artifact")
        destination = self.service.store.path("uploads", digest)
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
            return destination
        status, data = self.request("GET", f"/v1/artifacts/{digest}", timeout=UPLOAD_TIMEOUT_S)
        if status != 200:
            self._json_result(status, data)
            raise AgentError("artifact_unavailable", 409)
        if hashlib.sha256(data).hexdigest() != digest:
            raise AgentError("package_verification_failed", 409)
        atomic_bytes(destination, data, 0o400)
        return destination

    def should_forward(self, method: str, path: str) -> bool:
        coordinator = self.service.coordinator
        if coordinator is None or coordinator.is_primary:
            return False
        return not (method == "GET" and path in LOCAL_GETS)


UPLOAD_LIMIT = 512 * 1024 * 1024
