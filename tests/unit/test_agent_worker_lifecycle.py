"""The deployment agent stops job workers before migrations and restarts them after (#40)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent.backends import Backend, ComposeBackend  # noqa: E402
from deployment_agent.config import Config  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402

COMMANDS = {
    "drain": ["/bin/drain-web"],
    "restart": ["/bin/restart-web"],
    "stop": ["/bin/stop-web"],
    "worker_stop": ["/bin/stop-worker", "{operation}"],
    "worker_restart": ["/bin/start-worker", "{release}"],
}


class Recording:
    """A fake backend runner: records argv instead of executing anything."""

    def __init__(self):
        self.calls = []

    def __call__(self, arguments, *, env=None, cwd=None, code="command_failed"):
        self.calls.append([str(argument) for argument in arguments])


def directory_backend(commands):
    backend = Backend({"commands": commands, "active_link": "/srv/current", "storage_root": "/srv/state"}, None)
    backend.run = Recording()
    return backend


def test_directory_backend_stops_workers_after_drain_and_restarts_them_after_web():
    backend = directory_backend(COMMANDS)
    backend.drain("/srv/releases/old", "op-1")
    backend.start("/srv/releases/new", "op-1")
    assert [call[0] for call in backend.run.calls] == [
        "/bin/drain-web",
        "/bin/stop-worker",
        "/bin/restart-web",
        "/bin/start-worker",
    ]
    assert backend.run.calls[1] == ["/bin/stop-worker", "op-1"]
    assert backend.run.calls[3] == ["/bin/start-worker", "/srv/releases/new"]  # New release code.


def test_directory_backend_without_worker_hooks_keeps_the_previous_behaviour():
    commands = {name: COMMANDS[name] for name in ("drain", "restart", "stop")}
    backend = directory_backend(commands)
    backend.drain("/r", "op")
    backend.stop("/r", "op")
    backend.start("/r", "op")
    assert [call[0] for call in backend.run.calls] == ["/bin/drain-web", "/bin/stop-web", "/bin/restart-web"]


def test_compose_backend_overrides_and_cycles_the_worker_services(tmp_path, monkeypatch):
    config = {
        "commands": {name: COMMANDS[name] for name in ("drain", "restart", "stop")},
        "active_link": str(tmp_path / "current"),
        "storage_root": str(tmp_path),
        "maintenance_marker": str(tmp_path / "control" / "maintenance"),
        "compose": {
            "project": "sk",
            "env_file": "/srv/compose.env",
            "file": "/srv/compose.yaml",
            "web_service": "web",
            "migrator_service": "migrator",
            "worker_services": ["worker", "scheduler"],
        },
    }
    store = SimpleNamespace(root=tmp_path)

    class Values(dict):
        def environment(self, profile, release):
            return {"DATABASE_URL": "postgres://runtime@db/app"}

    backend = ComposeBackend(Values(config), store)
    recorder = Recording()
    backend.run = recorder
    rendered = {"services": {name: {"environment": {}} for name in ("web", "worker", "scheduler", "migrator")}}
    backend.capture = lambda *args, **kwargs: json.dumps(rendered).encode()
    release = tmp_path / "releases" / "r2"
    backend.drain(release, "op")
    backend.start(release, "op")
    joined = [" ".join(call) for call in recorder.calls]
    stop_web = next(index for index, call in enumerate(joined) if call.endswith("stop web"))
    stop_workers = next(index for index, call in enumerate(joined) if call.endswith("stop worker scheduler"))
    up_web = next(index for index, call in enumerate(joined) if call.endswith("--force-recreate web"))
    up_workers = next(index for index, call in enumerate(joined) if call.endswith("--force-recreate worker scheduler"))
    assert stop_web < stop_workers < up_web < up_workers
    override = json.loads((tmp_path / "compose-r2-runtime.json").read_text())
    assert set(override["services"]) == {"web", "worker", "scheduler"}
    assert override["services"]["worker"]["image"] == override["services"]["web"]["image"] == "skeleton-managed:r2"
    assert "command" not in override["services"]["worker"]  # The worker keeps its run_worker command.
    rendered["services"]["worker"]["environment"] = {"MIGRATION_DATABASE_URL": "postgres://owner@db/app"}
    with pytest.raises(AgentError, match="migration_secret_in_runtime"):
        backend.start(release, "op")


def private(path, text):
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def agent_config(tmp_path, **changes):
    values = {
        "storage_root": str(tmp_path / "state"),
        "releases_root": str(tmp_path / "releases"),
        "project_root": str(tmp_path / "base"),
        "active_link": str(tmp_path / "current"),
        "socket_path": str(tmp_path / "agent.sock"),
        "token_file": private(tmp_path / "token", "t" * 40),
        "runtime_env_file": private(tmp_path / "runtime.env", "DATABASE_URL=postgres://runtime:x@db/app\n"),
        "migration_env_file": private(tmp_path / "migration.env", "MIGRATION_DATABASE_URL=postgres://owner:y@db/app\n"),
        "build_env_file": private(tmp_path / "build.env", ""),
        "requirements_lock": str(tmp_path / "requirements.txt"),
        "wheelhouse": str(tmp_path / "wheels"),
        "maintenance_marker": str(tmp_path / "maintenance"),
        "project_paths": ["manage.py"],
        "approved_publishers": {"key": str(tmp_path / "key.pem")},
        "commands": dict(COMMANDS),
        "health_urls": ["http://127.0.0.1:8000/health/live/"],
        "backend": "directory",
        **changes,
    }
    path = Path(private(tmp_path / "agent.json", json.dumps(values)))
    return Config.load(path)


def test_configuration_accepts_worker_hooks_only_in_pairs(tmp_path):
    assert agent_config(tmp_path)["commands"]["worker_stop"] == COMMANDS["worker_stop"]
    lonely = {name: command for name, command in COMMANDS.items() if name != "worker_restart"}
    with pytest.raises(AgentError, match="incomplete_worker_commands"):
        agent_config(tmp_path, commands=lonely)
    relative = {**COMMANDS, "worker_stop": ["systemctl", "stop", "worker"]}
    with pytest.raises(AgentError, match="invalid_fixed_command"):
        agent_config(tmp_path, commands=relative)


@pytest.mark.parametrize(
    "workers", [["web"], ["migrator"], ["worker", "worker"], ["bad name"], "worker", [1]], ids=repr
)
def test_compose_worker_services_are_validated(tmp_path, workers):
    compose = {
        "file": "/srv/compose.yaml",
        "env_file": "/srv/compose.env",
        "dockerfile": "/srv/Dockerfile",
        "project": "sk",
        "web_service": "web",
        "migrator_service": "migrator",
        "base_image": "python:3.14-slim",
        "target_environment": {
            "python_full_version": "3.14.0",
            "python_version": "3.14",
            "platform_machine": "x86_64",
            "sys_platform": "linux",
        },
    }
    assert agent_config(tmp_path, backend="compose", compose={**compose, "worker_services": ["worker"]})
    with pytest.raises(AgentError, match="invalid_compose_worker_services"):
        agent_config(tmp_path, backend="compose", compose={**compose, "worker_services": workers})
