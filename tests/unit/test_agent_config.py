"""Private files, required configuration and the runtime/owner credential split of the agent (#247)."""

import json
import os

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent.config import Config, read_environment  # noqa: E402
from deployment_agent.storage import AgentError, private_file  # noqa: E402
from tests.unit.agent_helpers import (  # noqa: E402
    OWNER_DSN,
    RUNTIME_DSN,
    agent_config,
    config_values,
    load_config,
    write_private,
)

pytestmark = pytest.mark.posix


def refused(code, tmp_path, **overrides):
    with pytest.raises(AgentError) as raised:
        load_config(tmp_path, **overrides)
    return raised.value.code == code


def test_minimal_configuration_loads(tmp_path):
    config = load_config(tmp_path)
    assert config["backend"] == "directory" and config.get("missing", "default") == "default"


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o400])
def test_private_file_accepts_0600_and_0640(tmp_path, mode):
    path = write_private(tmp_path / "secret", "value", mode)
    assert private_file(path) == tmp_path / "secret"


def test_private_file_refuses_group_writable_world_readable_symlink_and_directory(tmp_path):
    for mode in (0o660, 0o620, 0o604, 0o602, 0o601, 0o666):
        path = write_private(tmp_path / f"loose-{mode:o}", "value", mode)
        with pytest.raises(AgentError, match="insecure_private_file"):
            private_file(path)
    target = write_private(tmp_path / "target", "value")
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(AgentError, match="insecure_private_file"):
        private_file(tmp_path / "link")
    (tmp_path / "folder").mkdir(mode=0o700)
    with pytest.raises(AgentError, match="insecure_private_file"):
        private_file(tmp_path / "folder")


def test_config_file_with_loose_permissions_is_refused(tmp_path):
    path = agent_config(tmp_path)
    os.chmod(path, 0o644)
    with pytest.raises(AgentError, match="invalid_agent_config"):  # The private_file error becomes this code.
        Config.load(path)
    os.chmod(path, 0o600)
    assert Config.load(path)


def test_malformed_config_file_is_refused(tmp_path):
    path = write_private(tmp_path / "agent.json", "{not json")
    with pytest.raises(AgentError, match="invalid_agent_config"):
        Config.load(path)
    path = write_private(tmp_path / "agent.json", "[]")
    with pytest.raises(AgentError, match="missing_agent_config"):
        Config.load(path)


@pytest.mark.parametrize("key", ["storage_root", "token_file", "migration_env_file", "commands", "backend"])
def test_missing_required_key_is_refused(tmp_path, key):
    values = config_values(tmp_path)
    del values[key]
    path = write_private(tmp_path / "agent.json", json.dumps(values))
    with pytest.raises(AgentError, match="missing_agent_config"):
        Config.load(path)


@pytest.mark.parametrize("key", ["storage_root", "releases_root", "active_link", "socket_path", "maintenance_marker"])
def test_relative_path_is_refused(tmp_path, key):
    assert refused("invalid_config_path", tmp_path, **{key: "relative/path"})


@pytest.mark.parametrize("value", ["skeleton", "skeleton/system", ".venv", "../x", "config/../..", "/abs", ".", ""])
def test_project_paths_refuse_core_venv_parent_and_absolute(tmp_path, value):
    assert refused("invalid_project_path", tmp_path, project_paths=["manage.py", value])


def test_project_paths_must_be_a_non_empty_list(tmp_path):
    assert refused("missing_project_paths", tmp_path, project_paths=[])
    assert refused("missing_project_paths", tmp_path, project_paths="manage.py")


def test_lifecycle_commands_require_absolute_executable(tmp_path):
    commands = {"drain": ["/bin/drain"], "restart": ["/bin/restart"], "stop": ["/bin/stop"]}
    assert refused("missing_lifecycle_commands", tmp_path, commands={"drain": ["/bin/drain"]})
    for bad in (["drain-web"], [], "/bin/drain", ["/bin/drain", ""], ["/bin/drain", "a\x00b"], ["/bin/drain", 1]):
        assert refused("invalid_fixed_command", tmp_path, commands={**commands, "drain": bad}), bad


def test_unknown_backend_and_publishers_are_refused(tmp_path):
    assert refused("invalid_backend", tmp_path, backend="kubernetes")
    assert refused("missing_publishers", tmp_path, approved_publishers={})


@pytest.mark.parametrize("profile", ["runtime_env_file", "build_env_file"])
def test_migration_secret_in_runtime_or_build_env_is_refused(tmp_path, profile):
    values = config_values(tmp_path)
    write_private(values[profile], f"DATABASE_URL={RUNTIME_DSN}\nMIGRATION_DATABASE_URL={OWNER_DSN}\n")
    path = write_private(tmp_path / "agent.json", json.dumps(values))
    with pytest.raises(AgentError, match="migration_secret_in_runtime"):
        Config.load(path)


def test_missing_migration_credentials_is_refused(tmp_path):
    values = config_values(tmp_path)
    for text in ("", "MIGRATION_DATABASE_URL=\n", "OTHER=x\n"):
        write_private(values["migration_env_file"], text)
        path = write_private(tmp_path / "agent.json", json.dumps(values))
        with pytest.raises(AgentError, match="missing_migration_credentials"):
            Config.load(path)


def test_missing_runtime_credentials_is_refused(tmp_path):
    values = config_values(tmp_path)
    write_private(values["runtime_env_file"], "SKELETON_ENV=production\n")
    path = write_private(tmp_path / "agent.json", json.dumps(values))
    with pytest.raises(AgentError, match="missing_runtime_credentials"):
        Config.load(path)


def test_shared_runtime_and_owner_dsn_is_refused(tmp_path):
    values = config_values(tmp_path)
    write_private(values["migration_env_file"], f"MIGRATION_DATABASE_URL={RUNTIME_DSN}\n")
    path = write_private(tmp_path / "agent.json", json.dumps(values))
    with pytest.raises(AgentError, match="shared_database_credentials"):
        Config.load(path)


@pytest.mark.parametrize(
    "profile, line",
    [
        ("runtime_env_file", "DATABASE_URL=sqlite:///srv/db.sqlite3"),
        ("migration_env_file", "MIGRATION_DATABASE_URL=mysql://owner:y@127.0.0.1/app"),
    ],
)
def test_non_postgres_dsn_is_refused(tmp_path, profile, line):
    values = config_values(tmp_path)
    write_private(values[profile], line + "\n")
    path = write_private(tmp_path / "agent.json", json.dumps(values))
    with pytest.raises(AgentError, match="unsupported_database"):
        Config.load(path)


def test_backup_env_must_target_same_database_and_role(tmp_path):
    other_password = OWNER_DSN.replace(":y@", ":other@")
    backup = write_private(tmp_path / "backup.env", f"MIGRATION_DATABASE_URL={other_password}\n")
    assert load_config(tmp_path, backup_env_file=backup)["backup_env_file"] == backup
    for dsn in (
        OWNER_DSN.replace("/app", "/other"),
        OWNER_DSN.replace("skeleton_owner", "skeleton_runtime"),
        OWNER_DSN.replace("postgres://", "mysql://"),
        "",
    ):
        write_private(tmp_path / "backup.env", f"MIGRATION_DATABASE_URL={dsn}\n")
        assert refused("backup_database_identity_mismatch", tmp_path, backup_env_file=backup), dsn
    assert refused("invalid_backup_environment_path", tmp_path, backup_env_file="backup.env")


@pytest.mark.parametrize("token", ["t" * 31, "é" * 40, "", "   " + "t" * 20 + "   "])
def test_token_must_be_ascii_and_at_least_32_characters(tmp_path, token):
    values = config_values(tmp_path)
    write_private(values["token_file"], token)
    path = write_private(tmp_path / "agent.json", json.dumps(values))
    with pytest.raises(AgentError, match="invalid_agent_token"):
        Config.load(path)


def test_environment_for_runtime_and_build_never_contains_owner_dsn(tmp_path):
    config = load_config(tmp_path)
    release = tmp_path / "releases" / "1-x"
    migration = config.environment("migration", release)
    assert migration["MIGRATION_DATABASE_URL"] == OWNER_DSN
    # Even an env file edited after the agent started cannot hand the owner DSN to the web.
    write_private(config["runtime_env_file"], f"DATABASE_URL={RUNTIME_DSN}\nMIGRATION_DATABASE_URL={OWNER_DSN}\n")
    write_private(config["build_env_file"], f"MIGRATION_DATABASE_URL={OWNER_DSN}\n")
    for profile in ("runtime", "build"):
        environment = config.environment(profile, release)
        assert "MIGRATION_DATABASE_URL" not in environment
        assert OWNER_DSN not in environment.values()
    assert config.environment("runtime", release)["DATABASE_URL"] == RUNTIME_DSN
    assert str(release / "archives") not in config.environment("runtime", release)["PYTHONPATH"]
    assert str(release / "archives") in migration["PYTHONPATH"]


def test_environment_does_not_inherit_process_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAK", "1")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", OWNER_DSN)
    config = load_config(tmp_path)
    for profile in ("runtime", "build"):
        environment = config.environment(profile, tmp_path / "release")
        assert "LEAK" not in environment and "MIGRATION_DATABASE_URL" not in environment
        assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin" and environment["HOME"] == config["storage_root"]


@pytest.mark.parametrize(
    "line, code",
    [
        ("NO_EQUALS_SIGN", "invalid_environment_file"),
        ("1BAD=x", "invalid_environment_key"),
        ("BAD-KEY=x", "invalid_environment_key"),
        ("export A=x", "invalid_environment_key"),
        ("A=two words", "invalid_environment_value"),
        ("A=$(id) x", "invalid_environment_value"),
    ],
)
def test_read_environment_rejects_line_without_equals_bad_key_and_multiword_value(tmp_path, line, code):
    path = write_private(tmp_path / "file.env", f"GOOD=1\n{line}\n")
    with pytest.raises(AgentError, match=code):
        read_environment(path)


def test_read_environment_does_not_expand_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/expanded")
    text = "# comment\n\nA=$HOME\nB='${HOME}/x'\nC=\"quoted value\"\nD=\nE=`id`\n"
    assert read_environment(write_private(tmp_path / "file.env", text)) == {
        "A": "$HOME",
        "B": "${HOME}/x",
        "C": "quoted value",
        "D": "",
        "E": "`id`",
    }


def test_read_environment_refuses_loose_file(tmp_path):
    with pytest.raises(AgentError, match="insecure_private_file"):
        read_environment(write_private(tmp_path / "file.env", "A=1\n", 0o644))
