"""The deployment agent reads the runtime database of a setup-configured instance (#148)."""

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent.config import effective_runtime_database  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402


def test_env_file_wins_and_the_instance_store_is_the_fallback(tmp_path, monkeypatch, settings):
    from skeleton.instance.writer import write_setting

    database, key = tmp_path / "instance.sqlite3", tmp_path / "keys" / "master.key"
    monkeypatch.setenv("SKELETON_INSTANCE_DB", str(database))
    monkeypatch.setenv("SKELETON_INSTANCE_KEY_FILE", str(key))
    write_setting(settings.BASE_DIR, "database_url", "postgres://skeleton_runtime:x@db:5432/app", source="test")
    bootstrap = {"SKELETON_INSTANCE_DB": str(database), "SKELETON_INSTANCE_KEY_FILE": str(key)}
    assert effective_runtime_database(bootstrap) == "postgres://skeleton_runtime:x@db:5432/app"
    assert effective_runtime_database({**bootstrap, "DATABASE_URL": "postgres://u:p@h/x"}) == "postgres://u:p@h/x"
    assert effective_runtime_database({}) is None
    with pytest.raises(AgentError):
        effective_runtime_database({**bootstrap, "SKELETON_INSTANCE_KEY_FILE": str(tmp_path / "other.key")})
