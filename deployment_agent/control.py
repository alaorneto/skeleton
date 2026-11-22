"""Control of the application services in containers without the Docker socket (#651).

The agent never talks to Docker. Each application container (web, worker, scheduler) runs the
launcher (``deployment_agent.launcher``), and the two sides talk through files on shared volumes:

``<control_root>/<service>/request.json``
    Written only by the agent, atomically: ``{"id", "action", "release", "operation"}``, with
    ``action`` ``stop`` or ``restart``. The application container mounts this directory read-only.
``<state_root>/<service>/state.json``
    Written only by the launcher of that service: ``{"id", "state", "release", "pid"}``, where
    ``id`` is the last request it applied and ``state`` is ``running``, ``stopped``, ``waiting``
    (no active release yet) or ``failed``. The agent mounts this directory read-only.

A request is applied once: the launcher remembers the ``id`` it applied, so reading the same file
again changes nothing. ``request_and_wait`` is what the agent's fixed hooks run (``drain``,
``stop``, ``restart``, ``worker_stop``, ``worker_restart``): it writes one request per service and
returns only when every service confirmed it, or fails with ``service_control_timeout`` or
``service_control_failed``.

Standard library only, no ``fcntl``: the launcher runs inside the runtime image, outside the
agent's own environment.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

__all__ = [
    "ACTIONS",
    "SERVICE",
    "ControlError",
    "read_json",
    "request_and_wait",
    "write_json",
    "write_request",
]

ACTIONS = ("stop", "restart")
SERVICE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
EXPECTED = {"stop": "stopped", "restart": "running"}
MAX_FILE_BYTES = 16 * 1024


class ControlError(ValueError):
    """A control request that did not complete; ``code`` is the agent error code."""

    def __init__(self, code: str, service: str = ""):
        super().__init__(f"{code} {service}".strip())
        self.code = code
        self.service = service


def write_json(path: Path, value: dict[str, Any], mode: int = 0o644) -> None:
    """Write ``value`` atomically (temporary file in the same directory, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    data = json.dumps(value, sort_keys=True).encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any] | None:
    """The object in ``path``, or ``None`` when it is missing, too large, a link or malformed."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def valid_request(value: dict[str, Any] | None) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", value["id"]) is not None
        and value.get("action") in ACTIONS
    )


def write_request(control_root: Path, service: str, action: str, *, release: str = "", operation: str = "") -> str:
    """Write the request of one service; returns its id."""
    if not SERVICE.fullmatch(service) or action not in ACTIONS:
        raise ControlError("invalid_service_control", service)
    request_id = uuid.uuid4().hex
    payload = {"id": request_id, "action": action, "release": release, "operation": operation}
    write_json(Path(control_root) / service / "request.json", payload)
    return request_id


def request_and_wait(
    control_root: Path,
    state_root: Path,
    services: list[str],
    action: str,
    *,
    timeout: float,
    release: str = "",
    operation: str = "",
    poll: float = 0.2,
) -> dict[str, dict[str, Any]]:
    """Ask every service for ``action`` and wait until each one confirms it; the states by service."""
    if not services or len(set(services)) != len(services):
        raise ControlError("invalid_service_control")
    pending = {
        service: write_request(control_root, service, action, release=release, operation=operation)
        for service in services
    }
    confirmed: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout
    while pending:
        for service, request_id in list(pending.items()):
            state = read_json(Path(state_root) / service / "state.json")
            if state is None or state.get("id") != request_id:
                continue
            if state.get("state") == EXPECTED[action]:
                confirmed[service] = state
                del pending[service]
            elif state.get("state") == "failed":
                raise ControlError("service_control_failed", service)
        if pending and time.monotonic() >= deadline:
            raise ControlError("service_control_timeout", sorted(pending)[0])
        if pending:
            time.sleep(poll)
    return confirmed
