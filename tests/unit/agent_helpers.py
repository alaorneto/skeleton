"""Factories shared by the deployment agent tests (#247).

Import only after ``pytest.importorskip("fcntl", ...)``: the agent runs on Linux or macOS.
"""

import json
import os
import socket
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from deployment_agent.config import Config
from deployment_agent.releases import owned_snapshot
from deployment_agent.service import DeploymentService
from deployment_agent.storage import Store, atomic_json, digest

TOKEN = "control-token-for-agent-tests-0123456789"  # 40 ASCII characters.
RUNTIME_DSN = "postgres://skeleton_runtime:x@127.0.0.1:5432/app"
OWNER_DSN = "postgres://skeleton_owner:y@127.0.0.1:5432/app"
COMMANDS = {"drain": ["/bin/drain-web"], "restart": ["/bin/restart-web"], "stop": ["/bin/stop-web"]}


def write_private(path, text, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


def config_values(tmp_path, **overrides):
    """Every key Config.load requires, with private env files and a 40-character token."""
    values = {
        "storage_root": str(tmp_path / "state"),
        "releases_root": str(tmp_path / "releases"),
        "project_root": str(tmp_path / "base"),
        "active_link": str(tmp_path / "current"),
        "socket_path": str(tmp_path / "control" / "agent.sock"),
        "token_file": write_private(tmp_path / "control-token", TOKEN),
        "runtime_env_file": write_private(tmp_path / "runtime.env", f"DATABASE_URL={RUNTIME_DSN}\n"),
        "migration_env_file": write_private(tmp_path / "migration.env", f"MIGRATION_DATABASE_URL={OWNER_DSN}\n"),
        "build_env_file": write_private(tmp_path / "build.env", ""),
        "requirements_lock": str(tmp_path / "requirements.lock"),
        "wheelhouse": str(tmp_path / "wheelhouse"),
        "maintenance_marker": str(tmp_path / "maintenance"),
        "project_paths": ["manage.py", "config"],
        "approved_publishers": {"org.example": str(tmp_path / "publisher.pem")},
        "commands": dict(COMMANDS),
        "health_urls": ["http://127.0.0.1:8000/health/live/"],
        "backend": "directory",
    }
    values.update(overrides)
    return values


def agent_config(tmp_path, **overrides):
    """Write the private agent configuration and return its path."""
    return Path(write_private(tmp_path / "agent.json", json.dumps(config_values(tmp_path, **overrides))))


def load_config(tmp_path, **overrides):
    return Config.load(agent_config(tmp_path, **overrides))


class Recording:
    """A fake backend runner: records argv instead of executing anything."""

    def __init__(self, effect=None):
        self.calls = []
        self.effect = effect

    def __call__(self, arguments, *, env=None, cwd=None, code="command_failed"):
        self.calls.append([str(argument) for argument in arguments])
        if self.effect:
            self.effect(arguments)


class Calls:
    """A fake backend method: records positional arguments, returns None."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)


def bare_service(tmp_path, *, inventory=None):
    """A DeploymentService without __init__: real journal, active release in tmp_path, fake backend."""
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "approved.txt").write_text("before")
    store = Store(tmp_path / "journal")
    inventory = inventory or {"generation": 0, "apps": [], "core": {"version": "0.6.0"}}
    atomic_json(
        store.root / "active.json",
        {"release": str(source), "inventory": inventory, "owned_files": owned_snapshot(source)},
    )
    service = DeploymentService.__new__(DeploymentService)
    service.store = store
    service.worker = None
    service.config = {
        "storage_root": str(store.root),
        "maintenance_marker": str(tmp_path / "maintenance"),
        "releases_root": str(tmp_path / "releases"),
        "minimum_free_bytes": 0,
        "token_file": write_private(tmp_path / "control-token", TOKEN),
        "approved_publishers": {"org.example": str(tmp_path / "publisher.pem")},
    }
    service.backend = SimpleNamespace(
        active=lambda: source,
        logging_to=lambda operation_id: nullcontext(),
        preflight=lambda *args: None,
        drain=Calls(),
        start=Calls(),
        cutover=Calls(),
        manage=Calls(),
        probe=lambda release: {"migrations": [], "applied": [], "targets": []},
        restore=Calls(),
    )
    service.source = source
    return service


def journal_operation(service, tmp_path, **fields):
    """A plan record and its operation in the journal, bound like submit() binds them."""
    source = service.source
    release = tmp_path / "release"
    release.mkdir(exist_ok=True)
    (release / "approved.txt").write_text("after")
    plan = {
        "id": str(uuid4()),
        "generation": 0,
        "action": "install",
        "release": str(release),
        "migrations": [],
        "migration_targets": [],
    }
    plan["digest"] = digest(plan)
    service.store.put(
        "plans",
        plan["id"],
        {
            "plan": plan,
            "previous_release": str(source),
            "owned_files": owned_snapshot(release),
            "previous_owned_files": owned_snapshot(source),
            "previous_inventory": service.state()["inventory"],
            "inventory": {"generation": 1, "apps": []},
        },
    )
    operation = {
        "id": str(uuid4()),
        "operation": "install",
        "action": "install",
        "state": "failed",
        "stage": "backup_ready",
        "generation": 0,
        "expected_generation": 0,
        "plan": plan,
        "plan_digest": plan["digest"],
        "binding": {"plan_id": plan["id"]},
        "backup": None,
        "release": str(release),
        "previous_release": str(source),
        "error": "migration_failed",
        "created_at": "2026-11-13T00:00:00+00:00",
        "updated_at": "2026-11-13T00:00:00+00:00",
        "traffic_released": False,
        "events": [{"stage": "approved", "at": "2026-11-13T00:00:00+00:00"}],
    }
    operation.update(fields)
    service.store.put("operations", operation["id"], operation)
    return operation


def http_request(method, path, *, body=None, headers=None, token=TOKEN, length=True):
    """Raw HTTP/1.1 request bytes, with a bearer token unless ``token`` is None."""
    lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    if body is not None and length and not any(key.lower() == "content-length" for key in headers or {}):
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b"")


def json_request(path, value, *, token=TOKEN, content_type="application/json"):
    return http_request(
        "POST", path, body=json.dumps(value).encode(), headers={"Content-Type": content_type}, token=token
    )


def exchange(handler_class, server, request):
    """Run one request through a handler over a socket pair; ``(status, headers, body bytes)``.

    The handler is the real ControlHandler/StatusHandler; only the listening socket is replaced,
    so the request line, headers and body go through http.server like on the Unix socket.
    """
    client, connection = socket.socketpair()
    with client, connection:
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        handler_class(connection, ("agent-test", 0), server)
        connection.shutdown(socket.SHUT_WR)
        response = b""
        while chunk := client.recv(65536):
            response += chunk
    head, _, body = response.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
    return int(lines[0].split()[1]), headers, body


def exchange_json(handler_class, server, request):
    status, headers, body = exchange(handler_class, server, request)
    return status, json.loads(body)
