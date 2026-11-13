"""Crash reconciliation, the rules of ``recover`` and verified backups of the agent (#247)."""

import json
from pathlib import Path

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent.backends import Backend  # noqa: E402
from deployment_agent.storage import AgentError, atomic_json, file_hash  # noqa: E402
from tests.unit.agent_helpers import OWNER_DSN, Recording, bare_service, journal_operation  # noqa: E402

pytestmark = pytest.mark.posix


@pytest.fixture
def service(tmp_path):
    return bare_service(tmp_path)


def marker(service):
    path = Path(service.config["maintenance_marker"])
    return json.loads(path.read_text()) if path.exists() else None


def test_reconcile_marks_queued_and_running_as_interrupted_and_sets_marker(service, tmp_path):
    queued = journal_operation(service, tmp_path, state="queued", stage="approved")
    running = journal_operation(service, tmp_path, state="running", stage="migration_started")
    service.reconcile_interrupted()
    for operation in (queued, running):
        row = service.store.get("operations", operation["id"])
        assert (row["state"], row["stage"], row["error"]) == ("interrupted", "interrupted", "agent_interrupted")
        assert row["events"][-1]["stage"] == "interrupted"
    assert marker(service)["stage"] == "interrupted"


@pytest.mark.parametrize("stage", ["committed", "traffic_release_started"])
def test_reconcile_marks_committed_but_unreleased_operation_as_interrupted(service, tmp_path, stage):
    operation = journal_operation(service, tmp_path, state="failed", stage=stage)
    service.reconcile_interrupted()
    assert service.store.get("operations", operation["id"])["state"] == "interrupted"
    assert marker(service) == {"operation": operation["id"], "stage": "interrupted"}


def test_reconcile_leaves_ready_and_failed_operations_untouched(service, tmp_path):
    rows = [
        journal_operation(service, tmp_path, state="succeeded", stage="ready", error=None),
        journal_operation(service, tmp_path, state="failed", stage="backup_failed"),
        journal_operation(service, tmp_path, state="restored", stage="restored", error=None),
    ]
    service.reconcile_interrupted()
    for row in rows:
        assert service.store.get("operations", row["id"]) == row
    assert marker(service) is None


def test_reconcile_sets_marker_on_active_pointer_conflict(service, tmp_path):
    service.backend.active = lambda: tmp_path / "elsewhere"
    service.reconcile_interrupted()
    assert marker(service) == {"stage": "active_state_conflict"}


def test_reconcile_sets_marker_on_drift_of_active_release(service):
    (service.source / "approved.txt").write_text("tampered")
    service.reconcile_interrupted()
    assert marker(service) == {"stage": "active_state_conflict"}


def recovery_refused(service, operation, action, code, **options):
    with pytest.raises(AgentError) as raised:
        service.recover(operation["id"], action, **options)
    return raised.value.code == code


def test_recover_refuses_unknown_action_and_non_recoverable_state(service, tmp_path):
    failed = journal_operation(service, tmp_path)
    assert recovery_refused(service, failed, "retry", "invalid_recovery_action")
    for state in ("succeeded", "queued", "running", "restored", "cancelled"):
        row = journal_operation(service, tmp_path, state=state)
        assert recovery_refused(service, row, "abort", "operation_not_recoverable"), state
    assert marker(service) is None  # Refused before entering maintenance.
    assert service.store.get("operations", failed["id"]) == failed


def test_recover_refuses_superseded_operation(service, tmp_path):
    operation = journal_operation(service, tmp_path)
    state = service.state()
    state["inventory"]["generation"] = 7
    atomic_json(service.store.root / "active.json", state)
    assert recovery_refused(service, operation, "abort", "operation_superseded")
    assert marker(service) is None


def assert_recovery_failed(service, operation, code):
    row = service.store.get("operations", operation["id"])
    assert (row["state"], row["stage"], row["error"]) == ("failed", "recovery_failed", code)
    assert row["events"][-1]["stage"] == "recovery_failed"
    assert marker(service)["operation"] == operation["id"]  # Maintenance stays on after a failed recovery.


@pytest.mark.parametrize("stage", ["migration_started", "migrated", "operator_completion"])
def test_abort_after_migration_started_is_refused(service, tmp_path, stage):
    events = [{"stage": "approved", "at": "t"}, {"stage": stage, "at": "t"}]
    operation = journal_operation(service, tmp_path, events=events)
    assert recovery_refused(service, operation, "abort", "database_restore_required")
    assert_recovery_failed(service, operation, "database_restore_required")
    assert service.backend.cutover.calls == []


def test_restore_without_backup_is_refused(service, tmp_path):
    operation = journal_operation(service, tmp_path, backup=None)
    assert recovery_refused(service, operation, "restore", "backup_not_available")
    assert_recovery_failed(service, operation, "backup_not_available")
    assert service.backend.restore.calls == []


@pytest.mark.parametrize("action", ["restore", "abort"])
def test_restore_or_abort_after_traffic_release_requires_acknowledgement(service, tmp_path, action):
    operation = journal_operation(service, tmp_path, traffic_released=True, backup=str(tmp_path / "backup.json"))
    assert recovery_refused(service, operation, action, "new_writes_require_explicit_reconciliation")
    assert_recovery_failed(service, operation, "new_writes_require_explicit_reconciliation")
    assert service.backend.restore.calls == [] and service.backend.cutover.calls == []


def test_abort_with_acknowledgement_returns_to_previous_release(service, tmp_path):
    operation = journal_operation(service, tmp_path, traffic_released=True)
    service.health = lambda inventory: None
    result = service.recover(operation["id"], "abort", acknowledge_new_writes=True)
    assert (result["state"], result["stage"]) == ("cancelled", "aborted")
    assert service.backend.cutover.calls == [(service.source,)]
    assert marker(service) is None


def test_complete_refuses_changed_migration_plan(service, tmp_path):
    migration = {"app_label": "notes", "name": "0002_extra", "atomic": True, "operations": []}
    operation = journal_operation(service, tmp_path)
    service.backend.probe = lambda release: {"migrations": [migration], "applied": [], "targets": []}
    assert recovery_refused(service, operation, "complete", "recovery_migration_plan_changed")
    assert_recovery_failed(service, operation, "recovery_migration_plan_changed")
    service.backend.probe = lambda release: {"migrations": [], "applied": [], "targets": [["notes", "0002"]]}
    operation = journal_operation(service, tmp_path)
    assert recovery_refused(service, operation, "complete", "recovery_migration_plan_changed")


def test_complete_refuses_drifted_release(service, tmp_path):
    operation = journal_operation(service, tmp_path)
    (Path(operation["release"]) / "approved.txt").write_text("tampered")
    assert recovery_refused(service, operation, "complete", "local_file_drift")
    assert_recovery_failed(service, operation, "local_file_drift")


def test_recovery_failure_keeps_maintenance_marker_and_journals_error(service, tmp_path):
    operation = journal_operation(service, tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("segredo")

    service.backend.manage = broken
    with pytest.raises(RuntimeError):
        service.recover(operation["id"], "abort")
    assert_recovery_failed(service, operation, "RuntimeError")
    assert "segredo" not in json.dumps(service.store.get("operations", operation["id"]))


def backend(tmp_path, **config):
    class Values(dict):
        def environment(self, profile, release):
            return {"MIGRATION_DATABASE_URL": OWNER_DSN}

    values = Values({"storage_root": str(tmp_path / "state"), "commands": {}, "active_link": str(tmp_path), **config})
    store = type("FakeStore", (), {"root": tmp_path / "state"})()
    (tmp_path / "state" / "backups").mkdir(parents=True)

    def dump(arguments):
        if Path(str(arguments[0])).name == "pg_dump":
            Path(str(arguments[-1])).write_bytes(b"PGDMP fake")

    result = Backend(values, store)
    result.run = Recording(dump)
    return result


def backup_with_resource(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.txt").write_text("a")
    result = backend(tmp_path, recovery_paths=[str(media)])
    path = Path(result.backup(tmp_path / "previous", "op-1"))
    return result, path, json.loads(path.read_text())


def test_backup_records_hashes_and_runs_only_dump_and_list(tmp_path):
    result, path, metadata = backup_with_resource(tmp_path)
    assert [Path(call[0]).name for call in result.run.calls] == ["pg_dump", "pg_restore"]
    assert result.run.calls[1][1] == "--list"
    assert metadata["database_sha256"] == file_hash(metadata["database"])
    assert metadata["resources"][0]["files"]["a.txt"]["type"] == "file"


def test_restore_refuses_backup_with_changed_database_hash(tmp_path):
    result, path, metadata = backup_with_resource(tmp_path)
    Path(metadata["database"]).write_bytes(b"PGDMP tampered")
    result.run.calls.clear()
    with pytest.raises(AgentError, match="backup_hash_conflict"):
        result.restore(path, tmp_path / "previous")
    assert result.run.calls == []  # No psql or pg_restore ran.


def test_restore_refuses_backup_with_changed_resource_hash(tmp_path):
    result, path, metadata = backup_with_resource(tmp_path)
    snapshot = Path(metadata["resources"][0]["snapshot"])
    (snapshot / "a.txt").write_text("changed")
    result.run.calls.clear()
    with pytest.raises(AgentError, match="backup_hash_conflict"):
        result.restore(path, tmp_path / "previous")
    (snapshot / "a.txt").write_text("a")
    (snapshot / "added.txt").write_text("new")
    with pytest.raises(AgentError, match="backup_hash_conflict"):
        result.restore(path, tmp_path / "previous")
    assert result.run.calls == []


def test_restore_of_verified_backup_resets_schema_then_restores(tmp_path):
    result, path, metadata = backup_with_resource(tmp_path)
    (tmp_path / "media" / "after-backup.txt").write_text("new")
    result.run.calls.clear()
    result.restore(path, tmp_path / "previous")
    assert [Path(call[0]).name for call in result.run.calls] == ["psql", "pg_restore"]
    assert "DROP SCHEMA IF EXISTS skeleton CASCADE" in result.run.calls[0]
    assert sorted(path.name for path in (tmp_path / "media").iterdir()) == ["a.txt"]


@pytest.mark.parametrize("kind", ["relative", "missing", "symlink", "nested_symlink"])
def test_backup_refuses_relative_missing_or_symlinked_recovery_path(tmp_path, kind):
    real = tmp_path / "real"
    real.mkdir()
    paths = {
        "relative": "media",
        "missing": str(tmp_path / "missing"),
        "symlink": str(tmp_path / "linked"),
        "nested_symlink": str(real),
    }
    if kind == "symlink":
        (tmp_path / "linked").symlink_to(real)
    if kind == "nested_symlink":
        (real / "escape").symlink_to(tmp_path)
    result = backend(tmp_path, recovery_paths=[paths[kind]])
    code = "recovery_path_symlink" if kind == "nested_symlink" else "invalid_recovery_path"
    with pytest.raises(AgentError, match=code):
        result.backup(tmp_path / "previous", "op-1")
