"""Inventory of the deployment agent: cached informative drift and a bounded journal read (#281)."""

import time
from contextlib import nullcontext
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import (
    releases,  # noqa: E402
    storage,  # noqa: E402
)
from deployment_agent import service as service_module  # noqa: E402
from deployment_agent.releases import owned_snapshot  # noqa: E402
from deployment_agent.service import DeploymentService  # noqa: E402
from deployment_agent.storage import AgentError, Store, atomic_json, digest  # noqa: E402

pytestmark = pytest.mark.posix


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


@pytest.fixture
def counted(monkeypatch):
    calls = []
    real = releases.inventory_files

    def inventory_files(root):
        calls.append(root)
        return real(root)

    monkeypatch.setattr(releases, "inventory_files", inventory_files)
    return calls


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: value[0])
    return value


def source_of(service):
    return Path(service.state()["release"])


def test_inventory_does_not_rehash_within_interval(approved_deployment, counted, clock):
    service, _, _ = approved_deployment
    first, second = service.inventory(), service.inventory()
    assert len(counted) == 1
    assert "error" not in first and first["drift_checked_at"] == second["drift_checked_at"]


def test_inventory_rehashes_after_interval_and_reports_drift(approved_deployment, counted, clock):
    service, _, _ = approved_deployment
    service.inventory()
    (source_of(service) / "approved.txt").write_text("tampered")
    assert "error" not in service.inventory()  # Still within the interval: the cached result.
    clock[0] += 301
    assert service.inventory()["error"] == "local_file_drift"
    assert len(counted) == 2


def test_zero_interval_disables_cache(approved_deployment, counted, clock):
    service, _, _ = approved_deployment
    service.config["drift_check_interval"] = 0
    service.inventory()
    service.inventory()
    assert len(counted) == 2


def test_cache_is_invalidated_when_active_release_changes(approved_deployment, counted, clock):
    service, _, _ = approved_deployment
    service.inventory()
    state = service.state()
    service._write_active(state["release"], state["inventory"], state["owned_files"])
    service.inventory()
    assert len(counted) == 2


def test_active_pointer_conflict_is_never_cached(approved_deployment, counted, clock, tmp_path):
    service, _, _ = approved_deployment
    service.inventory()
    original = service.backend.active
    service.backend.active = lambda: tmp_path / "elsewhere"
    assert service.inventory()["error"] == "active_pointer_conflict"
    service.backend.active = original
    assert "error" not in service.inventory()


def test_preview_still_detects_drift_right_after_cached_inventory(approved_deployment, clock):
    service, _, _ = approved_deployment
    service.inventory()
    (source_of(service) / "approved.txt").write_text("tampered")
    with pytest.raises(AgentError, match="local_file_drift"):
        service.preview("uninstall", app_ids=["org.example.app"])


def test_inventory_reports_drift_checked_at(approved_deployment, clock):
    service, _, _ = approved_deployment
    result = service.inventory()
    assert result["drift_checked_at"].endswith("+00:00")


def journal_with(store, count, *, state="succeeded"):
    for index in range(count):
        operation_id = str(uuid4())
        store.put(
            "operations",
            operation_id,
            {
                "id": operation_id,
                "state": state,
                "created_at": f"2026-01-01T00:00:{index:05d}",
                "binding": {"plan_id": "x"},
            },
        )


def test_operations_limit_parses_only_requested_files(tmp_path, monkeypatch):
    store = Store(tmp_path / "journal")
    journal_with(store, 200)
    calls = []
    real = storage.load_json
    monkeypatch.setattr(storage, "load_json", lambda path: calls.append(path) or real(path))
    rows = store.operations(limit=50)
    assert len(rows) == 50 and len(calls) == 50
    assert [row["created_at"] for row in rows] == sorted((row["created_at"] for row in rows), reverse=True)


def test_operations_without_limit_returns_everything_sorted(tmp_path):
    store = Store(tmp_path / "journal")
    journal_with(store, 12)
    rows = store.operations()
    assert len(rows) == 12
    assert [row["created_at"] for row in rows] == sorted((row["created_at"] for row in rows), reverse=True)


def test_ensure_available_refuses_old_interrupted_operation_with_index(approved_deployment):
    service, _, _ = approved_deployment
    journal_with(service.store, 3)
    old = str(uuid4())
    service.store.put(
        "operations", old, {"id": old, "state": "interrupted", "created_at": "2020-01-01T00:00:00", "binding": {}}
    )
    for _ in range(2):  # The second call reads the index the first one wrote.
        with pytest.raises(AgentError, match="recovery_or_operation_pending"):
            service.ensure_available()
    assert (service.store.root / "operations-index.json").exists()


def test_stale_or_missing_index_is_rebuilt(approved_deployment):
    service, _, _ = approved_deployment
    journal_with(service.store, 2)
    service.ensure_available()
    index = service.store.root / "operations-index.json"
    pending = str(uuid4())
    service.store.put(
        "operations", pending, {"id": pending, "state": "running", "created_at": "2026-02-01T00:00:00", "binding": {}}
    )
    with pytest.raises(AgentError):
        service.ensure_available()  # Missing from the index: read from the file.
    service.store.put(
        "operations", pending, {"id": pending, "state": "succeeded", "created_at": "2026-02-01T00:00:00", "binding": {}}
    )
    service.ensure_available()  # Changed mtime: the new state is read.
    index.write_text("not json")
    service.ensure_available()
    atomic_json(index, {pending: ["succeeded", 0]})
    service.store.put(
        "operations", pending, {"id": pending, "state": "queued", "created_at": "2026-02-01T00:00:00", "binding": {}}
    )
    with pytest.raises(AgentError):
        service.ensure_available()


def test_concurrent_inventory_calls_hash_once(approved_deployment, monkeypatch, clock):
    service, _, _ = approved_deployment
    started, release, calls = Event(), Event(), []
    real = releases.inventory_files

    def slow(root):
        calls.append(root)
        started.set()
        release.wait(5)
        return real(root)

    monkeypatch.setattr(releases, "inventory_files", slow)
    results = []
    threads = [Thread(target=lambda: results.append(service.inventory())) for _ in range(2)]
    threads[0].start()
    assert started.wait(5)
    threads[1].start()
    time.sleep(0.05)
    release.set()
    for thread in threads:
        thread.join(5)
    assert len(calls) == 1 and len(results) == 2
