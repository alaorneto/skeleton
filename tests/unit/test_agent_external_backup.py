"""External backup (PITR) for the updates (#683): a named restore point instead of a dump."""

from pathlib import Path

import pytest

from deployment_agent.backends import Backend
from deployment_agent.config import _validate_optional
from deployment_agent.storage import AgentError, load_json


class Config(dict):
    def environment(self, profile, release):
        return {"MIGRATION_DATABASE_URL": "postgres://skeleton_owner:x@db:5432/app"}


class Store:
    def __init__(self, root):
        self.root = root


def backend(tmp_path, *, answers, **backup):
    config = Config(
        backup={"mode": "external", "acknowledge_continuous_backup": True, **backup},
        psql="/usr/bin/psql",
        recovery_paths=[],
    )
    store = Store(tmp_path / "state")
    (store.root / "backups").mkdir(parents=True)  # The Store creates it in the agent.
    agent = Backend(config, store)
    calls = []

    def capture(arguments, *, env=None, cwd=None, code="command_failed"):
        calls.append((arguments[-1], code))
        return answers[code]

    agent.capture = capture
    agent.pg_environment = lambda release: {"PGHOST": "db"}
    return agent, calls


def test_the_configuration_needs_the_operator_to_own_the_continuous_backup():
    _validate_optional({})
    _validate_optional({"backup": {"mode": "pg_dump"}})
    _validate_optional({"backup": {"mode": "external", "acknowledge_continuous_backup": True}})
    for value, code in (
        ({"mode": "external"}, "external_backup_not_acknowledged"),
        ({"mode": "snapshot"}, "invalid_backup_config"),
        ({"mode": "external", "acknowledge_continuous_backup": True, "restore_point": "yes"}, "invalid_backup_config"),
        ({"mode": "pg_dump", "extra": 1}, "invalid_backup_config"),
    ):
        with pytest.raises(AgentError) as refused:
            _validate_optional({"backup": value})
        assert refused.value.code == code


def test_an_external_backup_records_the_restore_point_and_makes_no_dump(tmp_path):
    agent, calls = backend(tmp_path, answers={"restore_point_failed": b"0/3000090|0/30000C8\n"})
    path = agent.backup(tmp_path / "releases" / "1-old", "op-7")
    metadata = load_json(path)
    assert metadata["kind"] == "external" and metadata["restore_point"] == "skeleton-op-7"
    assert metadata["lsn"] == "0/30000C8" and metadata["previous_release"].endswith("1-old")
    assert not (Path(path).parent / "database.dump").exists()
    assert calls == [("SELECT pg_create_restore_point('skeleton-op-7'), pg_current_wal_lsn()", "restore_point_failed")]


def test_without_the_privilege_the_preflight_refuses_before_any_maintenance(tmp_path, monkeypatch):
    monkeypatch.setattr("os.access", lambda path, mode: True)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    agent, _ = backend(tmp_path, answers={"restore_point_check_failed": b"f\n"})
    with pytest.raises(AgentError) as refused:
        agent.preflight(tmp_path / "releases" / "1-old")
    assert refused.value.code == "restore_point_not_permitted"
    allowed, _ = backend(tmp_path / "ok", answers={"restore_point_check_failed": b"t\n"})
    allowed.preflight(tmp_path / "releases" / "1-old")


def test_the_restore_of_an_external_backup_is_the_operators(tmp_path):
    agent, _ = backend(tmp_path, answers={"restore_point_failed": b"0/1|0/2\n"})
    path = agent.backup(tmp_path / "releases" / "1-old", "op-8")
    with pytest.raises(AgentError) as refused:
        agent.restore(path, tmp_path / "releases" / "1-old")
    assert refused.value.code == "external_restore_required"
    assert refused.value.details[0]["component"] == "skeleton-op-8" and refused.value.details[0]["found"] == "0/2"
