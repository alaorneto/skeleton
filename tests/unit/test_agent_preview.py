"""Asynchronous preview of the deployment agent: plan states, preparation thread and recovery (#298)."""

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import service as service_module  # noqa: E402
from deployment_agent.releases import owned_snapshot  # noqa: E402
from deployment_agent.service import DeploymentService  # noqa: E402
from deployment_agent.storage import AgentError, Store, atomic_json, digest  # noqa: E402

pytestmark = pytest.mark.posix

# Keys of the plan the synchronous preview returned before #298 (same content, now prepared in a thread).
PLAN_KEYS = {
    "id",
    "generation",
    "action",
    "components",
    "files",
    "migrations",
    "applied_migrations",
    "migration_targets",
    "impact",
    "maintenance",
    "backup_required",
    "recovery",
    "release",
    "release_digest",
    "artifacts",
    "app_ids",
    "created_at",
    "backend",
    "presentation_changes",
    "resources",
    "digest",
}


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
def agent(approved_deployment, tmp_path, monkeypatch):
    """The approved fixture, extended with what a full preparation needs."""
    service, values, pending = approved_deployment
    state = service.state()
    state["inventory"] = {"generation": 0, "apps": []}
    atomic_json(service.store.root / "active.json", state)
    releases_root = tmp_path / "releases"
    releases_root.mkdir()
    service.config.update(
        {"releases_root": str(releases_root), "backend": "systemd", "active_link": str(tmp_path / "current")}
    )
    calls = []

    def export_infrastructure(release, path):
        Path(path).write_text("{}")

    service.backend.manage = lambda *args, **kwargs: calls.append(("manage", args[1]))
    service.backend.probe = lambda release: {"migrations": [], "applied": [], "targets": [], "impact": {}}
    service.backend.export_infrastructure = export_infrastructure

    def create_release(config, store, backend, inventory, artifacts, app_ids, release):
        release.mkdir()
        (release / "approved.txt").write_text("after")
        return {
            "generation": inventory["generation"] + 1,
            "core": {"version": "0.6.0", "digest": "d" * 64, "publisher": "org.example"},
            "apps": [],
            "retained_schema_paths": [],
        }

    monkeypatch.setattr(service_module, "create_release", create_release)
    monkeypatch.setattr(service_module, "check_plan_dependencies", lambda *args, **kwargs: None)
    return SimpleNamespace(
        service=service, values=values, pending=pending, releases=releases_root, calls=calls, monkeypatch=monkeypatch
    )


def start(agent):
    summary = agent.service.preview("uninstall", app_ids=["org.example.app"])
    return summary, agent.pending[-1]


def run(worker):
    return worker.target(*worker.args)


def test_preview_returns_immediately_with_preparing_plan(agent):
    summary, worker = start(agent)
    assert set(summary) == {"id", "state"} and summary["state"] == "preparing"
    record = agent.service.store.get("plans", summary["id"])
    assert record["state"] == "preparing"
    assert record["request"] == {"action": "uninstall", "artifacts": [], "app_ids": ["org.example.app"]}
    assert not list(agent.releases.iterdir())  # Nothing was prepared yet.
    assert agent.calls == []  # check_skeleton_schema runs in the thread.
    assert worker.args == (summary["id"],)


def test_prepared_plan_becomes_ready_with_same_content(agent):
    summary, worker = start(agent)
    plan = run(worker)
    status = agent.service.plan_status(summary["id"])
    assert status["state"] == "ready" and status["plan"] == plan
    assert set(plan) == PLAN_KEYS and plan["id"] == summary["id"]
    assert plan["digest"] == digest({key: value for key, value in plan.items() if key != "digest"})
    assert plan["generation"] == 0 and plan["action"] == "uninstall" and plan["app_ids"] == ["org.example.app"]
    assert Path(plan["release"]).name == f"1-{summary['id']}" and Path(plan["release"]).is_dir()
    assert ("manage", ["check_skeleton_schema"]) in agent.calls
    record = agent.service.store.get("plans", summary["id"])
    assert record["inventory"]["generation"] == 1 and record["previous_inventory"]["generation"] == 0


def test_failed_preparation_is_recorded_and_leaves_no_release(agent):
    def conflicting(config, store, backend, inventory, artifacts, app_ids, release):
        release.mkdir()
        (release / "partial").write_text("x")
        raise AgentError("wheel_set_conflict", 409)

    agent.monkeypatch.setattr(service_module, "create_release", conflicting)
    summary, worker = start(agent)
    assert run(worker) is None
    status = agent.service.plan_status(summary["id"])
    assert status["state"] == "failed" and status["error"] == "wheel_set_conflict"
    assert not list(agent.releases.iterdir())
    agent.service.ensure_available()  # A failed plan does not block the next preview.


def test_unexpected_preparation_error_records_class_without_message(agent, capsys):
    def broken(*args):
        raise RuntimeError("segredo")

    agent.monkeypatch.setattr(service_module, "create_release", broken)
    summary, worker = start(agent)
    run(worker)
    status = agent.service.plan_status(summary["id"])
    assert (status["state"], status["error"]) == ("failed", "RuntimeError")
    assert "segredo" not in json.dumps(status) and "segredo" not in capsys.readouterr().err


def test_dependency_problem_is_still_synchronous(agent):
    details = [{"code": "missing_dependency", "component": "a", "dependency": "b", "required": ">=1"}]

    def unsatisfied(*args, **kwargs):
        raise AgentError("dependency_unsatisfied", 409, details)

    agent.monkeypatch.setattr(service_module, "check_plan_dependencies", unsatisfied)
    with pytest.raises(AgentError) as raised:
        agent.service.preview("uninstall", app_ids=["org.example.app"])
    assert raised.value.code == "dependency_unsatisfied" and raised.value.details == details
    assert [record.get("state") for _, record, _ in agent.service.store.plans()] == [None]  # Only the fixture plan.
    assert agent.pending == []


def test_second_preview_while_preparing_is_refused(agent):
    start(agent)
    with pytest.raises(AgentError) as raised:
        agent.service.preview("uninstall", app_ids=["org.example.app"])
    assert (raised.value.code, raised.value.status) == ("recovery_or_operation_pending", 409)
    assert len(agent.pending) == 1


def test_preparation_rechecks_after_lock_was_released(agent):
    summary, worker = start(agent)
    (Path(agent.service.state()["release"]) / "approved.txt").write_text("tampered")
    run(worker)
    status = agent.service.plan_status(summary["id"])
    assert (status["state"], status["error"]) == ("failed", "local_file_drift")
    assert not list(agent.releases.iterdir())


def test_preparation_of_a_record_that_is_no_longer_preparing_does_nothing(agent):
    summary, worker = start(agent)
    record = agent.service.store.get("plans", summary["id"])
    agent.service.store.put("plans", summary["id"], {**record, "state": "failed", "error": "agent_interrupted"})
    assert run(worker) is None
    assert agent.service.plan_status(summary["id"])["error"] == "agent_interrupted"
    assert not list(agent.releases.iterdir())


@pytest.mark.parametrize("state", ["preparing", "failed"])
def test_submit_refuses_plan_that_is_not_ready(agent, state):
    values = agent.values
    record = agent.service.store.get("plans", values["plan_id"])
    agent.service.store.put("plans", values["plan_id"], {**record, "state": state})
    with pytest.raises(AgentError) as raised:
        agent.service.submit(**values)
    assert (raised.value.code, raised.value.status) == ("plan_not_ready", 409)
    assert not agent.service.store.path("operations", values["operation_id"]).exists()


def test_plan_record_without_state_is_ready(agent):
    values = agent.values
    assert "state" not in agent.service.store.get("plans", values["plan_id"])
    status = agent.service.plan_status(values["plan_id"])
    assert status["state"] == "ready" and status["plan"]["digest"] == values["plan_digest"]
    agent.service.submit(**values)  # Records written before #298 can still be approved.
    assert len(agent.pending) == 1


def test_reconcile_fails_interrupted_preparation_without_maintenance(agent):
    summary, _ = start(agent)
    partial = agent.releases / f"1-{summary['id']}"
    partial.mkdir()
    (partial / "half").write_text("x")
    other = agent.releases / f"1-{uuid4()}"
    other.mkdir()
    agent.service.reconcile_interrupted()
    status = agent.service.plan_status(summary["id"])
    assert (status["state"], status["error"]) == ("failed", "agent_interrupted")
    assert not partial.exists() and other.exists()
    assert not Path(agent.service.config["maintenance_marker"]).exists()
    agent.service.ensure_available()


def test_plan_status_never_exposes_inventory_or_owned_files(agent):
    summary, worker = start(agent)
    for _ in range(2):
        status = agent.service.plan_status(summary["id"])
        assert set(status) <= {"id", "state", "created_at", "request", "plan", "error", "details"}
        serialized = json.dumps(status)
        for key in ("owned_files", "inventory", "previous_release", "previous_inventory", "source_owned_digest"):
            assert f'"{key}"' not in serialized
        run(worker)


def test_plan_status_of_unknown_or_malformed_id(agent):
    with pytest.raises(AgentError, match="not_found"):
        agent.service.plan_status(str(uuid4()))
    with pytest.raises(AgentError, match="invalid_uuid"):
        agent.service.plan_status("../active")
