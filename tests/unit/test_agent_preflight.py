"""Optional agent configuration is checked before maintenance and in the preview (#246)."""

import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import service as service_module  # noqa: E402
from deployment_agent.backends import Backend  # noqa: E402
from deployment_agent.config import _validate_optional  # noqa: E402
from deployment_agent.releases import owned_snapshot  # noqa: E402
from deployment_agent.service import DeploymentService  # noqa: E402
from deployment_agent.storage import AgentError, Store, atomic_json, digest  # noqa: E402

pytestmark = pytest.mark.posix
SH = sys.executable  # Any absolute executable stands for the backup tools.


# Same approved plan as tests/unit/test_deployment_journal.py (test modules are not importable).
@pytest.fixture
def approved_deployment(tmp_path, monkeypatch):
    source, release = tmp_path / "source", tmp_path / "release"
    source.mkdir()
    release.mkdir()
    (source / "approved.txt").write_text("before")
    (release / "approved.txt").write_text("after")
    source_owned, target_owned = owned_snapshot(source), owned_snapshot(release)
    store = Store(tmp_path / "journal")
    plan = {
        "id": str(uuid4()),
        "generation": 0,
        "action": "install",
        "release": str(release),
        "artifacts": [],
        "migrations": [],
        "applied_migrations": [],
        "migration_targets": [],
    }
    plan["digest"] = digest(plan)
    previous_inventory = {"generation": 0}
    target_inventory = {"generation": 1}
    store.put(
        "plans",
        plan["id"],
        {
            "plan": plan,
            "previous_release": str(source),
            "source_owned_digest": digest(source_owned),
            "owned_files": target_owned,
            "previous_owned_files": source_owned,
            "previous_inventory": previous_inventory,
            "inventory": target_inventory,
        },
    )
    atomic_json(
        store.root / "active.json",
        {
            "release": str(source),
            "inventory": previous_inventory,
            "owned_files": source_owned,
        },
    )
    service = DeploymentService.__new__(DeploymentService)
    service.store = store
    service.worker = None
    service.config = {
        "maintenance_marker": str(tmp_path / "maintenance"),
        "releases_root": str(tmp_path),
        "minimum_free_bytes": 0,
    }
    service.backend = SimpleNamespace(
        active=lambda: source,
        probe=lambda path: {"migrations": [], "applied": [], "targets": []},
        drain=lambda *args: None,
        backup=lambda *args: str(tmp_path / "backup.json"),
        logging_to=lambda operation_id: nullcontext(),
        preflight=lambda *args: None,
    )
    pending = []

    class DeferredWorker:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            pending.append(self)

    monkeypatch.setattr(service_module, "threading", SimpleNamespace(Thread=DeferredWorker))
    values = {
        "plan_id": plan["id"],
        "plan_digest": plan["digest"],
        "generation": 0,
        "operation_id": str(uuid4()),
        "actor_id": str(uuid4()),
        "request_id": str(uuid4()),
    }
    return service, values, pending


class RecordingBackend(Backend):
    """The real preflight and logging; every effect is recorded instead of executed."""

    def __init__(self, config, store, source, tmp_path):
        super().__init__(config, store)
        self.source, self.tmp_path, self.effects = source, tmp_path, []

    def active(self):
        return self.source

    def probe(self, release):
        return {"migrations": [], "applied": [], "targets": []}

    def drain(self, release, operation):
        self.effects.append("drain")

    def backup(self, release, operation):
        self.effects.append("backup")
        return str(self.tmp_path / "backup.json")


def with_backend(approved, tmp_path, **config):
    service, values, pending = approved
    base = {"storage_root": str(service.store.root), "pg_dump": SH, "pg_restore": SH, "psql": SH}
    service.backend = RecordingBackend({**base, **config}, service.store, Path(service.state()["release"]), tmp_path)
    return service, values, pending


def run_execution(service, values, pending):
    service.submit(**values)
    pending[0].target(*pending[0].args)
    return service.store.get("operations", values["operation_id"])


def assert_refused_before_maintenance(service, operation, code):
    assert (operation["state"], operation["error"]) == ("failed", code)
    stages = [event["stage"] for event in operation["events"]]
    assert "maintenance_entering" not in stages and "drained" not in stages
    assert not Path(service.config["maintenance_marker"]).exists()
    assert service.backend.effects == []


def test_invalid_recovery_path_fails_before_maintenance(approved_deployment, tmp_path):
    service, values, pending = with_backend(approved_deployment, tmp_path, recovery_paths=[str(tmp_path / "missing")])
    operation = run_execution(service, values, pending)
    assert_refused_before_maintenance(service, operation, "invalid_recovery_path")


def test_missing_backup_tool_fails_before_maintenance(approved_deployment, tmp_path):
    tool = tmp_path / "pg_dump"
    tool.write_text("not executable")
    tool.chmod(0o600)
    for value in (str(tmp_path / "absent"), str(tool)):
        service, values, pending = with_backend(approved_deployment, tmp_path, pg_dump=value)
        values = {**values, "operation_id": str(uuid4()), "request_id": str(uuid4())}
        pending.clear()
        operation = run_execution(service, values, pending)
        assert_refused_before_maintenance(service, operation, "backup_tool_unavailable")
        service.store.path("operations", values["operation_id"]).unlink()


def test_recovery_path_with_symlink_fails_before_maintenance(approved_deployment, tmp_path):
    folder = tmp_path / "media"
    folder.mkdir()
    (folder / "link").symlink_to(tmp_path)
    service, values, pending = with_backend(approved_deployment, tmp_path, recovery_paths=[str(folder)])
    operation = run_execution(service, values, pending)
    assert_refused_before_maintenance(service, operation, "recovery_path_symlink")


def test_preview_refuses_invalid_recovery_path_without_creating_release(approved_deployment, tmp_path):
    service, _, _ = with_backend(approved_deployment, tmp_path, recovery_paths=[str(tmp_path / "missing")])
    service.config["releases_root"] = str(tmp_path / "releases")
    with pytest.raises(AgentError, match="invalid_recovery_path"):
        service.preview("uninstall", app_ids=["org.example.app"])
    assert not (tmp_path / "releases").exists()


@pytest.mark.parametrize(
    "key, value, code",
    [
        ("recovery_paths", "/srv/media", "invalid_recovery_path"),
        ("recovery_paths", ["relative/media"], "invalid_recovery_path"),
        ("smoke_token_file", "smoke.token", "invalid_smoke_token_path"),
        ("pg_dump", "pg_dump", "invalid_tool_path"),
        ("socket_gid", "1000", "invalid_socket_gid"),
        ("socket_gid", -1, "invalid_socket_gid"),
        ("socket_gid", True, "invalid_socket_gid"),
        (
            "status_http",
            {"port": 8080, "public_url": "https://status.example.test", "host": "0.0.0.0"},
            "insecure_status_listener",
        ),
        ("status_http", {"public_url": "https://status.example.test"}, "insecure_status_listener"),
        ("status_http", {"port": 8080, "public_url": "ftp://status.example.test"}, "insecure_status_listener"),
        ("catalogue_sources", [{"url": "https://catalog.example.test"}], "invalid_catalogue_sources"),
        ("command_timeout", 0, "invalid_numeric_setting"),
        ("minimum_free_bytes", True, "invalid_numeric_setting"),
    ],
)
def test_config_rejects_malformed_optional_keys(key, value, code):
    with pytest.raises(AgentError) as raised:
        _validate_optional({key: value})
    assert raised.value.code == code


def test_config_accepts_minimal_configuration_without_optional_keys():
    _validate_optional({})
    _validate_optional(
        {
            "recovery_paths": ["/srv/media"],
            "smoke_token_file": "/etc/skeleton/smoke.token",
            "socket_gid": 0,
            "status_http": {"port": 8443, "public_url": "https://status.example.test"},
            "catalogue_sources": [{"id": "main"}],
            "command_timeout": 1800,
            "health_timeout": 2.5,
        }
    )


def test_smoke_token_with_loose_permissions_is_refused(tmp_path):
    token = tmp_path / "smoke.token"
    token.write_text("token")
    token.chmod(0o644)
    backend = Backend({"pg_dump": SH, "pg_restore": SH, "psql": SH, "smoke_token_file": str(token)}, None)
    with pytest.raises(AgentError, match="insecure_private_file"):
        backend.preflight(tmp_path)
    token.chmod(0o640)
    backend.preflight(tmp_path)  # Group read is what docs/deployment.md asks for.


def test_preflight_passes_with_valid_paths_and_tools(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "file.txt").write_text("x")
    Backend({"pg_dump": SH, "pg_restore": SH, "psql": SH, "recovery_paths": [str(media)]}, None).preflight(tmp_path)


def test_valid_configuration_keeps_the_stage_order(approved_deployment, tmp_path):
    service, values, pending = with_backend(approved_deployment, tmp_path)
    service.finish = lambda operation, record: service.stage(operation, "finished_by_test")
    operation = run_execution(service, values, pending)
    stages = [event["stage"] for event in operation["events"]]
    assert stages[1:] == ["maintenance_entering", "drained", "backup_ready", "migration_started", "finished_by_test"]
    assert service.backend.effects == ["drain", "backup"]
