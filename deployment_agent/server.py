"""Bounded Unix-socket HTTP control API and independent scoped read-only status UI."""

import hmac
import html
import json
import os
import secrets
import signal
import socket
import threading
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn, UnixStreamServer
from types import FrameType
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

from .storage import AgentError, atomic_bytes, canonical, identifier, log_internal_error, private_file
from .types import JSON

if TYPE_CHECKING:
    from .backends import ComposeBackend
    from .service import DeploymentService


class StatusTickets:
    def __init__(self, service: "DeploymentService") -> None:
        self.service = service
        self.key_path = service.store.root / "status-signing.key"
        if not self.key_path.exists():
            atomic_bytes(self.key_path, secrets.token_bytes(32))
        self.key = private_file(self.key_path).read_bytes()
        self.epoch_path = service.store.root / "status-epoch"
        if not self.epoch_path.exists():
            atomic_bytes(self.epoch_path, b"0")

    def issue(self, operation_id: str) -> str | None:
        status = self.service.config.get("status_http")
        if not status:
            return None
        body = {
            "operation": identifier(operation_id),
            "expires": int(time.time()) + 300,
            "epoch": int(self.epoch_path.read_text()),
            "nonce": secrets.token_hex(12),
        }
        payload = urlsafe_b64encode(canonical(body)).rstrip(b"=")
        signature = urlsafe_b64encode(hmac.digest(self.key, payload, "sha256")).rstrip(b"=")
        ticket = (payload + b"." + signature).decode("ascii")
        return status["public_url"].rstrip("/") + "/" + operation_id + "/?" + urlencode({"ticket": ticket})

    def verify(self, operation_id: str, ticket: str) -> None:
        try:
            if len(ticket) > 1024:
                raise ValueError
            payload, signature = ticket.encode("ascii").split(b".")
            supplied = urlsafe_b64decode(signature + b"=" * (-len(signature) % 4))
            if not hmac.compare_digest(supplied, hmac.digest(self.key, payload, "sha256")):
                raise ValueError
            body = json.loads(urlsafe_b64decode(payload + b"=" * (-len(payload) % 4)))
            if (
                body["operation"] != identifier(operation_id)
                or type(body["expires"]) is not int
                or not int(time.time()) < body["expires"] <= int(time.time()) + 300
                or body["epoch"] != int(self.epoch_path.read_text())
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeError, AgentError):
            raise AgentError("invalid_status_ticket", 403) from None

    def revoke(self) -> None:
        atomic_bytes(self.epoch_path, str(int(self.epoch_path.read_text()) + 1).encode())


class QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # The control UnixHTTPServer or the status ThreadingHTTPServer; serve() gives both
    # ``service`` and ``tickets``.
    server: Any

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format: str, *arguments: Any) -> None:
        # Never log request URL, bearer control token, scoped ticket, body or subprocess output.
        pass

    def send_json(self, value: object, status: int = 200) -> None:
        body = canonical(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class ControlHandler(QuietHandler):
    def authenticate(self) -> None:
        expected = private_file(self.server.service.config["token_file"]).read_text().strip()
        supplied = self.headers.get("Authorization", "")
        # Bytes, not str: compare_digest refuses non-ASCII text with TypeError, which became a 500 (#280).
        if (
            not expected
            or len(expected) < 32
            or not hmac.compare_digest(supplied.encode("utf-8", "replace"), ("Bearer " + expected).encode("utf-8"))
        ):
            raise AgentError("unauthorized", 401)

    def body(self, *, upload: bool = False) -> Any:
        if self.headers.get("Transfer-Encoding"):
            raise AgentError("unsupported_transfer_encoding")
        try:
            size = int(self.headers["Content-Length"])
        except (ValueError, TypeError):
            raise AgentError("content_length_required", 411) from None
        maximum = self.server.service.config.get("max_upload_bytes", 128 * 1024 * 1024) if upload else 1024 * 1024
        if size < 0 or size > maximum:
            raise AgentError("request_too_large", 413)
        value = self.rfile.read(size)
        if len(value) != size:
            raise AgentError("incomplete_request")
        if upload:
            if self.headers.get_content_type() != "application/octet-stream":
                raise AgentError("invalid_content_type", 415)
            return value
        if self.headers.get_content_type() != "application/json":
            raise AgentError("invalid_content_type", 415)
        try:
            body = json.loads(value)
        except ValueError:
            raise AgentError("invalid_json") from None
        if not isinstance(body, dict):
            raise AgentError("invalid_json")
        return body

    def operation(self, operation_id: str) -> JSON:
        result = self.server.service.operation(operation_id)
        url = self.server.tickets.issue(result["id"])
        if url:
            result["status_url"] = url
        return result

    def dispatch(self) -> Any:
        self.authenticate()
        path = urlsplit(self.path)
        if path.query or path.fragment:
            raise AgentError("invalid_control_path")
        if self.command == "GET":
            return self._get(path.path)
        if self.command == "POST":
            if path.path == "/uploads":
                return self.server.service.upload(self.body(upload=True))
            body = self.body()  # Read and validated before the route, as for any POST.
            route = self.POST_ROUTES.get(path.path)
            if route:
                return route(self, body)
        raise AgentError("not_found", 404)

    def _get(self, path: str) -> Any:
        if path == "/inventory":
            return self.server.service.inventory()
        if path.startswith("/operations/"):
            return self.operation(identifier(path.removeprefix("/operations/")))
        if path.startswith("/plans/"):
            return self.server.service.plan_status(identifier(path.removeprefix("/plans/")))
        raise AgentError("not_found", 404)

    def _status_ticket(self, body: JSON) -> JSON:
        if set(body) != {"operation_id"}:
            raise AgentError("invalid_status_ticket_fields")
        return {"status_url": self.server.tickets.issue(identifier(body["operation_id"]))}

    def _preview(self, body: JSON) -> JSON:
        if set(body) - {"action", "artifacts", "app_ids"}:
            raise AgentError("unknown_fields")
        return self.server.service.preview(body.get("action"), body.get("artifacts", []), body.get("app_ids", []))

    def _submit(self, body: JSON) -> JSON:
        if set(body) != {"plan_id", "plan_digest", "generation", "operation_id", "actor_id", "request_id"}:
            raise AgentError("invalid_submit_fields")
        result = self.server.service.submit(**body)
        url = self.server.tickets.issue(result["id"])
        if url:
            result["status_url"] = url
        return result

    def _catalog_check(self, body: JSON) -> JSON:
        if body:
            raise AgentError("unknown_fields")
        return self.server.service.check_updates()

    def _catalog_fetch(self, body: JSON) -> JSON:
        if set(body) != {"component_id", "version", "source_id"}:
            raise AgentError("invalid_catalogue_selection")
        return self.server.service.fetch_release(**body)

    POST_ROUTES: dict[str, Callable[["ControlHandler", JSON], JSON]] = {
        "/status-ticket": _status_ticket,
        "/preview": _preview,
        "/submit": _submit,
        "/catalog/check": _catalog_check,
        "/catalog/fetch": _catalog_fetch,
    }

    def handle_request(self) -> None:
        try:
            self.send_json(self.dispatch())
        except AgentError as error:
            body = {"error": error.code}
            if error.details:
                body["details"] = error.details
            self.send_json(body, error.status)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as error:  # noqa: BLE001 - unexpected errors become a 500 with class and stack only
            log_internal_error(error)
            self.send_json({"error": "agent_internal_error"}, 500)
        finally:
            self.close_connection = True

    do_GET = handle_request
    do_POST = handle_request


class StatusHandler(QuietHandler):
    def do_GET(self) -> None:
        try:
            path = urlsplit(self.path)
            base = urlsplit(self.server.service.config["status_http"]["public_url"]).path.rstrip("/")
            prefix = base + "/"
            if not path.path.startswith(prefix):
                raise AgentError("not_found", 404)
            operation_id = identifier(path.path[len(prefix) :].strip("/"))
            query = parse_qs(path.query, strict_parsing=True)
            if set(query) - {"ticket", "format"} or len(query.get("ticket", [])) != 1:
                raise AgentError("invalid_status_ticket", 403)
            self.server.tickets.verify(operation_id, query["ticket"][0])
            keys = ("id", "state", "stage", "generation", "error", "created_at", "updated_at")
            if self.server.service.store.path("operations", operation_id).exists():
                operation = self.server.service.operation(operation_id)
                summary = {key: operation[key] for key in keys}
            else:
                # A valid preview ticket may precede submission of the approved plan.
                summary = dict.fromkeys(keys)
                summary.update(id=operation_id, state="awaiting_submission", stage="awaiting_submission")
            if query.get("format") == ["json"]:
                self.send_json(summary)
                return
            rows = "".join(
                f"<tr><th>{html.escape(key)}</th><td>{html.escape('—' if value is None else str(value))}</td></tr>"
                for key, value in summary.items()
            )
            body = f'<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="3"><title>Operação de deployment</title><h1>Operação de deployment</h1><p>Canal independente, somente leitura. Atualização a cada 3 segundos; autorização expira em cinco minutos.</p><table>{rows}</table></html>'.encode()
            self.send_response(200)
            for key, value in (
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                (
                    "Content-Security-Policy",
                    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                ),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
            ):
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (AgentError, ValueError) as error:
            self.send_json(
                {"error": error.code if isinstance(error, AgentError) else "invalid_request"},
                error.status if isinstance(error, AgentError) else 400,
            )
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as error:  # noqa: BLE001 - unexpected errors become a JSON 500 with class and stack only
            # Class and stack only, never the raw traceback with its message (#280).
            log_internal_error(error)
            self.send_json({"error": "agent_internal_error"}, 500)
        finally:
            self.close_connection = True


def install_signal_handlers(server: Any) -> Callable[[int, FrameType | None], None]:
    """SIGTERM/SIGINT stop accepting requests; serve() then waits for the running operation (#280)."""

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        # shutdown() blocks until serve_forever() returns: it must run outside the main thread.
        threading.Thread(target=server.shutdown, daemon=True, name="deployment-shutdown").start()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, request_shutdown)
    return request_shutdown


class UnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    service: "DeploymentService"  # Set by serve().
    tickets: StatusTickets


def serve(service: "DeploymentService") -> None:
    path = Path(service.config["socket_path"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if path.exists():
        if not path.is_socket():
            raise AgentError("socket_path_collision")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink(missing_ok=True)
            else:
                raise AgentError("agent_already_running", 409)
    service.reconcile_interrupted()
    if service.config.get("backend") == "compose":
        cast("ComposeBackend", service.backend).remove_stale_overrides()  # Abrupt stop overrides hold secrets (#245).
    tickets = StatusTickets(service)
    previous_umask = os.umask(0o117)  # The socket is born at most 0660 (#280).
    try:
        server = UnixHTTPServer(str(path), ControlHandler)
    finally:
        os.umask(previous_umask)
    server.service = service
    server.tickets = tickets
    os.chmod(path, 0o660)
    if service.config.get("socket_gid") is not None:
        os.chown(path, -1, service.config["socket_gid"])
    status_server: Any = None  # ThreadingHTTPServer with service and tickets, when configured.
    if service.config.get("status_http"):
        options = service.config["status_http"]
        if options.get("host", "127.0.0.1") not in ("127.0.0.1", "::1") or urlsplit(
            options.get("public_url", "")
        ).scheme not in ("https", "http"):
            raise AgentError("insecure_status_listener")
        status_server = ThreadingHTTPServer((options.get("host", "127.0.0.1"), options["port"]), StatusHandler)
        status_server.service = service
        status_server.tickets = tickets
        threading.Thread(target=status_server.serve_forever, daemon=True, name="deployment-status").start()
    if threading.current_thread() is threading.main_thread():
        install_signal_handlers(server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if status_server:
            status_server.shutdown()
            status_server.server_close()
        worker = getattr(service, "worker", None)
        if worker is not None and worker.is_alive():
            worker.join()  # The operation in progress reaches its final state before the agent exits.
        path.unlink(missing_ok=True)
