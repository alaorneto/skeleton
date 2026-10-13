"""Bounded Unix-socket HTTP control API and independent scoped read-only status UI."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import socket
from socketserver import ThreadingMixIn, UnixStreamServer
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .storage import AgentError, atomic_bytes, canonical, identifier, private_file


class StatusTickets:
    def __init__(self, service):
        self.service = service
        self.key_path = service.store.root / "status-signing.key"
        if not self.key_path.exists():
            atomic_bytes(self.key_path, secrets.token_bytes(32))
        self.key = private_file(self.key_path).read_bytes()
        self.epoch_path = service.store.root / "status-epoch"
        if not self.epoch_path.exists():
            atomic_bytes(self.epoch_path, b"0")

    def issue(self, operation_id):
        status = self.service.config.get("status_http")
        if not status:
            return None
        body = {"operation": identifier(operation_id), "expires": int(time.time()) + 300, "epoch": int(self.epoch_path.read_text()), "nonce": secrets.token_hex(12)}
        payload = urlsafe_b64encode(canonical(body)).rstrip(b"=")
        signature = urlsafe_b64encode(hmac.digest(self.key, payload, "sha256")).rstrip(b"=")
        ticket = (payload + b"." + signature).decode("ascii")
        return status["public_url"].rstrip("/") + "/" + operation_id + "/?" + urlencode({"ticket": ticket})

    def verify(self, operation_id, ticket):
        try:
            if len(ticket) > 1024:
                raise ValueError
            payload, signature = ticket.encode("ascii").split(b".")
            supplied = urlsafe_b64decode(signature + b"=" * (-len(signature) % 4))
            if not hmac.compare_digest(supplied, hmac.digest(self.key, payload, "sha256")):
                raise ValueError
            body = json.loads(urlsafe_b64decode(payload + b"=" * (-len(payload) % 4)))
            if body["operation"] != identifier(operation_id) or type(body["expires"]) is not int or not int(time.time()) < body["expires"] <= int(time.time()) + 300 or body["epoch"] != int(self.epoch_path.read_text()):
                raise ValueError
        except (ValueError, TypeError, KeyError, UnicodeError, AgentError):
            raise AgentError("invalid_status_ticket", 403) from None

    def revoke(self):
        atomic_bytes(self.epoch_path, str(int(self.epoch_path.read_text()) + 1).encode())


class QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *arguments):
        # Never log request URL, bearer control token, scoped ticket, body or subprocess output.
        pass

    def send_json(self, value, status=200):
        body = canonical(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class ControlHandler(QuietHandler):
    def authenticate(self):
        expected = private_file(self.server.service.config["token_file"]).read_text().strip()
        supplied = self.headers.get("Authorization", "")
        if not expected or len(expected) < 32 or not hmac.compare_digest(supplied, "Bearer " + expected):
            raise AgentError("unauthorized", 401)

    def body(self, *, upload=False):
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

    def operation(self, operation_id):
        result = self.server.service.operation(operation_id)
        url = self.server.tickets.issue(result["id"])
        if url:
            result["status_url"] = url
        return result

    def dispatch(self):
        self.authenticate()
        service = self.server.service
        path = urlsplit(self.path)
        if path.query or path.fragment:
            raise AgentError("invalid_control_path")
        if self.command == "GET":
            if path.path == "/inventory":
                return service.inventory()
            if path.path.startswith("/operations/"):
                return self.operation(identifier(path.path.removeprefix("/operations/")))
        elif self.command == "POST":
            if path.path == "/uploads":
                return service.upload(self.body(upload=True))
            body = self.body()
            if path.path == "/status-ticket":
                if set(body) != {"operation_id"}:
                    raise AgentError("invalid_status_ticket_fields")
                return {"status_url": self.server.tickets.issue(identifier(body["operation_id"]))}
            if path.path == "/preview":
                if set(body) - {"action", "artifacts", "app_ids"}:
                    raise AgentError("unknown_fields")
                return service.preview(body.get("action"), body.get("artifacts", []), body.get("app_ids", []))
            if path.path == "/submit":
                if set(body) != {"plan_id", "plan_digest", "generation", "operation_id", "actor_id", "request_id"}:
                    raise AgentError("invalid_submit_fields")
                result = service.submit(**body)
                url = self.server.tickets.issue(result["id"])
                if url:
                    result["status_url"] = url
                return result
            if path.path == "/catalog/check":
                if body:
                    raise AgentError("unknown_fields")
                return service.check_updates()
            if path.path == "/catalog/fetch":
                if set(body) != {"component_id", "version", "source_id"}:
                    raise AgentError("invalid_catalogue_selection")
                return service.fetch_release(**body)
        raise AgentError("not_found", 404)

    def handle_request(self):
        try:
            self.send_json(self.dispatch())
        except AgentError as error:
            self.send_json({"error": error.code}, error.status)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self.send_json({"error": "agent_internal_error"}, 500)
        finally:
            self.close_connection = True

    do_GET = handle_request
    do_POST = handle_request


class StatusHandler(QuietHandler):
    def do_GET(self):
        try:
            path = urlsplit(self.path)
            base = urlsplit(self.server.service.config["status_http"]["public_url"]).path.rstrip("/")
            prefix = base + "/"
            if not path.path.startswith(prefix):
                raise AgentError("not_found", 404)
            operation_id = identifier(path.path[len(prefix):].strip("/"))
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
            rows = "".join(f"<tr><th>{html.escape(key)}</th><td>{html.escape(str(value or '—'))}</td></tr>" for key, value in summary.items())
            body = f'<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="3"><title>Operação de deployment</title><h1>Operação de deployment</h1><p>Canal independente, somente leitura. Atualização a cada 3 segundos; autorização expira em cinco minutos.</p><table>{rows}</table></html>'.encode()
            self.send_response(200)
            for key, value in (("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"), ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"), ("Referrer-Policy", "no-referrer"), ("X-Content-Type-Options", "nosniff")):
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except (AgentError, ValueError) as error:
            self.send_json({"error": error.code if isinstance(error, AgentError) else "invalid_request"}, error.status if isinstance(error, AgentError) else 400)
        finally:
            self.close_connection = True


class UnixHTTPServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    block_on_close = False


def serve(service):
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
    tickets = StatusTickets(service)
    server = UnixHTTPServer(str(path), ControlHandler)
    server.service = service
    server.tickets = tickets
    os.chmod(path, 0o660)
    if service.config.get("socket_gid") is not None:
        os.chown(path, -1, service.config["socket_gid"])
    status_server = None
    if service.config.get("status_http"):
        options = service.config["status_http"]
        if options.get("host", "127.0.0.1") not in ("127.0.0.1", "::1") or urlsplit(options.get("public_url", "")).scheme not in ("https", "http"):
            raise AgentError("insecure_status_listener")
        status_server = ThreadingHTTPServer((options.get("host", "127.0.0.1"), options["port"]), StatusHandler)
        status_server.service = service
        status_server.tickets = tickets
        threading.Thread(target=status_server.serve_forever, daemon=True, name="deployment-status").start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if status_server:
            status_server.shutdown()
            status_server.server_close()
        path.unlink(missing_ok=True)
