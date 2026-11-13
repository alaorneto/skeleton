"""Unix-socket control transport. No deployment commands or owner credentials in web."""

import http.client
import json
import socket
from pathlib import Path
from uuid import UUID


class AgentError(ValueError):
    def __init__(self, code, status=None, details=()):
        self.code = code
        self.status = status
        self.details = tuple(details)
        super().__init__(code)


_DETAIL_FIELDS = ("code", "component", "dependency", "required", "found")
_SUGGESTION_FIELDS = ("source_id", "component_id", "version", "digest")


def _bounded_text(value, maximum=200):
    return value if isinstance(value, str) and 0 < len(value) <= maximum and value.isprintable() else None


def sanitize_details(raw):
    """Keep only the closed problem shape the agent emits; drop anything else."""
    if not isinstance(raw, list):
        return ()
    details = []
    for item in raw[:64]:
        if not isinstance(item, dict) or _bounded_text(item.get("code"), 100) is None:
            continue
        detail = {key: item[key] for key in _DETAIL_FIELDS if _bounded_text(item.get(key)) is not None}
        path = item.get("path")
        if isinstance(path, list) and all(_bounded_text(step) for step in path[:32]):
            detail["path"] = path[:32]
        suggestions = []
        for suggestion in item.get("suggestions", [])[:8] if isinstance(item.get("suggestions"), list) else []:
            if isinstance(suggestion, dict) and all(_bounded_text(suggestion.get(key)) for key in _SUGGESTION_FIELDS):
                suggestions.append({key: suggestion[key] for key in _SUGGESTION_FIELDS})
        if suggestions:
            detail["suggestions"] = suggestions
        details.append(detail)
    return tuple(details)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


# Deadlines per kind of call (#250): reads of the agent journal and the queueing of an approved
# plan are short; uploads, previews and catalog fetches keep the instance deadline.
STATUS_TIMEOUT = 10
SUBMIT_TIMEOUT = 30


class AgentClient:
    def __init__(self, socket_path, token_file, *, timeout=1800, max_upload_bytes=128 * 1024 * 1024):
        if (
            not socket_path
            or not token_file
            or not Path(socket_path).is_absolute()
            or not Path(token_file).is_absolute()
        ):
            raise AgentError("agent_not_configured")
        self.socket_path = str(socket_path)
        self.token_file = Path(token_file)
        self.timeout = timeout
        self.max_upload_bytes = max_upload_bytes

    def _request(self, method, path, body=None, *, upload=False, timeout=None):
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
            connection = UnixConnection(self.socket_path, min(self.timeout, timeout) if timeout else self.timeout)
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
                    raise AgentError(code, response.status, sanitize_details(result.get("details")))
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
        return self._request("GET", "/inventory", timeout=STATUS_TIMEOUT)

    def preview(self, action, artifacts=None, app_ids=None):
        return self._request(
            "POST", "/preview", {"action": action, "artifacts": artifacts or [], "app_ids": app_ids or []}
        )

    def submit(self, plan_id, plan_digest, generation, operation_id, actor_id, request_id):
        return self._request(
            "POST",
            "/submit",
            {
                "plan_id": str(plan_id),
                "plan_digest": plan_digest,
                "generation": generation,
                "operation_id": str(operation_id),
                "actor_id": str(actor_id),
                "request_id": str(request_id),
            },
            timeout=SUBMIT_TIMEOUT,
        )

    def operation(self, operation_id):
        try:
            operation_id = str(UUID(str(operation_id)))
        except (ValueError, TypeError):
            raise AgentError("invalid_uuid") from None
        return self._request("GET", f"/operations/{operation_id}", timeout=STATUS_TIMEOUT)

    def plan(self, plan_id):
        """State of a preview in preparation (#298): preparing, ready (with the plan) or failed."""
        try:
            plan_id = str(UUID(str(plan_id)))
        except (ValueError, TypeError):
            raise AgentError("invalid_uuid") from None
        return self._request("GET", f"/plans/{plan_id}", timeout=STATUS_TIMEOUT)

    def status_ticket(self, operation_id):
        return self._request(
            "POST", "/status-ticket", {"operation_id": str(UUID(str(operation_id)))}, timeout=STATUS_TIMEOUT
        )

    def check_updates(self):
        return self._request("POST", "/catalog/check")

    def fetch_release(self, component_id, version, source_id):
        return self._request(
            "POST", "/catalog/fetch", {"component_id": component_id, "version": version, "source_id": source_id}
        )
