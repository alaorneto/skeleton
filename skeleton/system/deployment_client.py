"""Unix-socket control transport. No deployment commands or owner credentials in web."""

import http.client
import json
from pathlib import Path
import socket
from uuid import UUID


class AgentError(ValueError):
    def __init__(self, code, status=None):
        self.code = code
        self.status = status
        super().__init__(code)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class AgentClient:
    def __init__(self, socket_path, token_file, *, timeout=1800, max_upload_bytes=128 * 1024 * 1024):
        if not socket_path or not token_file or not Path(socket_path).is_absolute() or not Path(token_file).is_absolute():
            raise AgentError("agent_not_configured")
        self.socket_path = str(socket_path)
        self.token_file = Path(token_file)
        self.timeout = timeout
        self.max_upload_bytes = max_upload_bytes

    def _request(self, method, path, body=None, *, upload=False):
        try:
            token = self.token_file.read_text().strip()
            if not token or len(token) < 32:
                raise AgentError("invalid_agent_token")
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if method == "POST":
                if upload:
                    headers["Content-Type"] = "application/octet-stream"
                    encoded = body
                else:
                    headers["Content-Type"] = "application/json"
                    encoded = json.dumps(body or {}, separators=(",", ":")).encode()
            else:
                encoded = None
            connection = UnixConnection(self.socket_path, self.timeout)
            try:
                connection.request(method, path, body=encoded, headers=headers)
                response = connection.getresponse()
                payload = response.read(16 * 1024 * 1024 + 1)
                if len(payload) > 16 * 1024 * 1024:
                    raise AgentError("agent_response_too_large")
                result = json.loads(payload)
                if not isinstance(result, dict):
                    raise AgentError("invalid_agent_response")
                if response.status >= 400:
                    code = result.get("error", "agent_request_failed")
                    if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 100:
                        code = "agent_request_failed"
                    raise AgentError(code, response.status)
                return result
            finally:
                connection.close()
        except AgentError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise AgentError("agent_unavailable") from None

    def upload(self, path_or_bytes):
        if isinstance(path_or_bytes, bytes):
            data = path_or_bytes
        else:
            path = Path(path_or_bytes)
            if not path.is_file() or path.stat().st_size > self.max_upload_bytes:
                raise AgentError("invalid_upload_size")
            data = path.read_bytes()
        if not data or len(data) > self.max_upload_bytes:
            raise AgentError("invalid_upload_size")
        return self._request("POST", "/uploads", data, upload=True)

    def inventory(self):
        return self._request("GET", "/inventory")

    def preview(self, action, artifacts=None, app_ids=None):
        return self._request("POST", "/preview", {"action": action, "artifacts": artifacts or [], "app_ids": app_ids or []})

    def submit(self, plan_id, plan_digest, generation, operation_id, actor_id, request_id):
        return self._request("POST", "/submit", {"plan_id": str(plan_id), "plan_digest": plan_digest, "generation": generation, "operation_id": str(operation_id), "actor_id": str(actor_id), "request_id": str(request_id)})

    def operation(self, operation_id):
        try:
            operation_id = str(UUID(str(operation_id)))
        except (ValueError, TypeError):
            raise AgentError("invalid_uuid") from None
        return self._request("GET", f"/operations/{operation_id}")

    def status_ticket(self, operation_id):
        return self._request("POST", "/status-ticket", {"operation_id": str(UUID(str(operation_id)))})

    def check_updates(self):
        return self._request("POST", "/catalog/check")

    def fetch_release(self, component_id, version, source_id):
        return self._request("POST", "/catalog/fetch", {"component_id": component_id, "version": version, "source_id": source_id})
