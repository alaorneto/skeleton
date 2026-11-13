"""Offline retention of the deployment agent: what prune lists, removes and never touches (#245)."""

import os
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent.releases import freeze  # noqa: E402
from deployment_agent.service import DeploymentService  # noqa: E402
from deployment_agent.storage import AgentError, Store, atomic_json  # noqa: E402

pytestmark = pytest.mark.posix
OLD = time.time() - 30 * 86400


class Agent:
    """A DeploymentService over a scratch storage root, with the backend's run recorded."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "releases"
        self.root.mkdir()
        self.store = Store(tmp_path / "journal")
        self.service = DeploymentService.__new__(DeploymentService)
        self.service.store = self.store
        self.service.config = {
            "maintenance_marker": str(tmp_path / "maintenance"),
            "releases_root": str(self.root),
            "docker": "/usr/bin/docker",
        }
        self.calls = []
        self.service.backend = SimpleNamespace(image=lambda path: f"skeleton-managed:{path.name}", run=self.run)
        self.active = self.release("active")
        self.generation = 5
        atomic_json(
            self.store.root / "active.json",
            {"release": str(self.active), "inventory": {"generation": 5}, "owned_files": {}},
        )

    def run(self, arguments, **kwargs):
        self.calls.append([str(argument) for argument in arguments])

    def release(self, name):
        path = self.root / name
        path.mkdir()
        (path / "manage.py").write_text("")
        return path

    def plan(self, release, *, age=OLD, generation=4, inventory=5):
        plan_id = str(uuid4())
        record = {
            "plan": {"id": plan_id, "release": str(release), "generation": generation},
            "previous_release": str(self.active),
            "inventory": {"generation": inventory},
        }
        self.store.put("plans", plan_id, record)
        os.utime(self.store.path("plans", plan_id), (age, age))
        return plan_id

    def operation(self, state, release, previous, plan_id=None, *, created="2026-11-01T00:00:00+00:00"):
        operation_id = str(uuid4())
        self.store.put(
            "operations",
            operation_id,
            {
                "id": operation_id,
                "state": state,
                "binding": {"plan_id": plan_id or self.plan(release)},
                "release": str(release),
                "previous_release": str(previous),
                "created_at": created,
            },
        )
        return operation_id

    def backup(self, operation_id, *, age=OLD):
        path = self.store.root / "backups" / operation_id
        path.mkdir(parents=True)
        (path / "dump.sql").write_text("data")
        os.utime(path, (age, age))
        return path

    def prune(self, **options):
        return self.service.prune(**options)


@pytest.fixture
def agent(tmp_path):
    return Agent(tmp_path)


def test_prune_dry_run_changes_nothing(agent):
    preview = agent.release("unsubmitted")
    agent.plan(preview)
    result = agent.prune(plan_days=7)
    assert [item["path"] for item in result["releases"]] == [str(preview.resolve())]
    assert result["apply"] is False and len(result["plans"]) == 1
    assert preview.exists() and agent.calls == []


def test_prune_removes_unsubmitted_preview_older_than_limit(agent):
    preview = agent.release("unsubmitted")
    plan_id = agent.plan(preview)
    result = agent.prune(plan_days=7, apply=True)
    assert not preview.exists() and not agent.store.path("plans", plan_id).exists()
    assert result["images"] == ["skeleton-managed:unsubmitted"]
    assert agent.calls == [["/usr/bin/docker", "image", "rm", "skeleton-managed:unsubmitted"]]


def test_prune_keeps_recent_unsubmitted_preview(agent):
    preview = agent.release("fresh")
    plan_id = agent.plan(preview, age=time.time())
    agent.prune(plan_days=7, apply=True)
    assert preview.exists() and agent.store.path("plans", plan_id).exists()


def test_prune_keeps_active_release_and_previous_of_last_success(agent):
    older, previous = agent.release("older"), agent.release("previous")
    agent.operation("succeeded", older, agent.root, created="2026-10-01T00:00:00+00:00")
    agent.operation("succeeded", previous, older, created="2026-10-15T00:00:00+00:00")
    agent.operation("succeeded", agent.active, previous, created="2026-11-01T00:00:00+00:00")
    result = agent.prune(apply=True)
    assert agent.active.exists() and previous.exists()
    assert not older.exists()
    assert [item["path"] for item in result["releases"]] == [str(older.resolve())]


def test_prune_never_touches_release_or_backup_of_failed_operation(agent):
    target = agent.release("target")
    failed = agent.operation("failed", target, agent.active)
    backup = agent.backup(failed)
    later = agent.release("later")
    agent.operation("succeeded", later, agent.active, created="2026-11-02T00:00:00+00:00")
    agent.prune(plan_days=0, backup_days=0, apply=True)
    assert target.exists() and agent.active.exists() and backup.exists()


def test_superseded_failed_operation_no_longer_protects(agent):
    target = agent.release("superseded")
    plan_id = agent.plan(target, generation=1, inventory=2)  # Active generation is 5: recover refuses it.
    failed = agent.operation(
        "failed", target, agent.root / "gone", plan_id=plan_id, created="2026-10-01T00:00:00+00:00"
    )
    agent.operation("succeeded", agent.active, target, created="2026-11-01T00:00:00+00:00")
    assert (
        agent.service._recoverable(
            agent.store.get("operations", failed), agent.store.get("plans", plan_id), agent.service.state()
        )
        is False
    )


def test_prune_is_refused_during_maintenance_or_pending_operation(agent):
    marker = agent.service.config["maintenance_marker"]
    open(marker, "w").close()
    with pytest.raises(AgentError, match="recovery_or_operation_pending"):
        agent.prune()
    os.unlink(marker)
    for state in ("queued", "running", "interrupted"):
        operation_id = agent.operation(state, agent.release(f"pending-{state}"), agent.active)
        with pytest.raises(AgentError, match="recovery_or_operation_pending"):
            agent.prune()
        agent.store.path("operations", operation_id).unlink()


def test_prune_keeps_backups_unless_backup_days_is_given(agent):
    done = agent.operation("succeeded", agent.release("done"), agent.active, created="2026-10-01T00:00:00+00:00")
    agent.operation("succeeded", agent.release("newest"), agent.active, created="2026-11-01T00:00:00+00:00")
    backup = agent.backup(done)
    assert agent.prune(apply=True)["backups"] == [] and backup.exists()
    assert agent.prune(backup_days=7, apply=True)["backups"] == [str(backup)]
    assert not backup.exists()


def test_prune_skips_release_path_outside_releases_root(agent, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    agent.plan(outside)
    result = agent.prune(apply=True)
    assert outside.exists()
    assert result["skipped"] == [{"path": str(outside), "reason": "outside_releases_root"}]


def test_prune_removes_frozen_release(agent):
    preview = agent.release("frozen")
    (preview / "pkg").mkdir()
    (preview / "pkg" / "module.py").write_text("VALUE = 1\n")
    freeze(preview)
    agent.plan(preview)
    agent.prune(apply=True)
    assert not preview.exists()


def test_image_removal_failure_is_reported_not_fatal(agent):
    agent.plan(agent.release("unsubmitted"))

    def fail(arguments, **kwargs):
        raise AgentError("image_remove_failed", 409)

    agent.service.backend.run = fail
    result = agent.prune(apply=True)
    assert result["errors"] == [{"image": "skeleton-managed:unsubmitted", "code": "image_remove_failed"}]


def test_control_api_has_no_prune_route():
    """prune is offline only: the control socket answers 404 for it, GET or POST (#245)."""
    from deployment_agent.server import ControlHandler

    for command in ("GET", "POST"):
        handler = ControlHandler.__new__(ControlHandler)
        handler.server = SimpleNamespace(service=SimpleNamespace(prune=lambda **kwargs: pytest.fail("reached prune")))
        handler.command, handler.path = command, "/prune"
        handler.authenticate = lambda: None
        handler.body = lambda **kwargs: {}
        with pytest.raises(AgentError) as raised:
            handler.dispatch()
        assert (raised.value.code, raised.value.status) == ("not_found", 404)
