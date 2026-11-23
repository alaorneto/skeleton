"""Only provisioned configuration authorizes paths, secrets and subprocesses."""

import json
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from .coordination import validate_cluster
from .github import REPOSITORY
from .github import valid_settings as github_settings_valid
from .storage import AgentError, private_file
from .types import JSON, StrPath

ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def effective_runtime_setting(runtime: Mapping[str, str], name: str) -> str | None:
    """A runtime setting as the web will read it: the env file, else the instance SQLite (#148)."""
    if runtime.get(name):
        return runtime[name]
    database, key = runtime.get("SKELETON_INSTANCE_DB"), runtime.get("SKELETON_INSTANCE_KEY_FILE")
    if not database or not key:
        return None
    from skeleton.instance.resolve import LayeredEnviron
    from skeleton.instance.store import InstanceStore

    try:
        stored = InstanceStore(Path(database)).settings()
        return LayeredEnviron({}, stored, Path(key)).get(name)
    except Exception as exc:  # noqa: BLE001 - unreadable store or key: refuse, never guess
        raise AgentError("instance_configuration_unreadable") from exc


def effective_runtime_database(runtime: Mapping[str, str]) -> str | None:
    """The runtime DATABASE_URL the web will use: the env file, else the instance SQLite (#148).

    A setup-configured instance keeps its configuration in the instance SQLite (secrets
    encrypted with its master key); the runtime env file then carries only the bootstrap paths
    ``SKELETON_INSTANCE_DB`` and ``SKELETON_INSTANCE_KEY_FILE``.
    """
    return effective_runtime_setting(runtime, "DATABASE_URL")


def installed_bundled_app_ids(config: "Config | Mapping[str, Any]") -> tuple[str, ...]:
    """App IDs of the first-party apps the runtime installs (SKELETON_BUNDLED_APPS, #254)."""
    path = config.get("runtime_env_file")
    if not path:
        return ()
    value = effective_runtime_setting(read_environment(path), "SKELETON_BUNDLED_APPS") or ""
    from django.core.exceptions import ImproperlyConfigured

    from skeleton.conf.bundled import bundled_app_ids

    try:
        return bundled_app_ids(value.split(","))
    except ImproperlyConfigured:
        raise AgentError("unknown_bundled_app", 409) from None


NUMERIC_SETTINGS = ("command_timeout", "health_timeout", "catalogue_timeout", "max_upload_bytes", "minimum_free_bytes")
TOOL_SETTINGS = ("python", "pg_dump", "pg_restore", "psql", "docker")


def _absolute(value: object) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value and Path(value).is_absolute()


def _valid_source(source: JSON, values: JSON) -> bool:
    """At most one of path, url and github (#498); a GitHub source pins an approved publisher.

    A source without any keeps loading, as before: the check marks it invalid_catalogue_source.
    """
    if sum(key in source for key in ("path", "url", "github")) > 1:
        return False
    channels = source.get("channels", ["stable"])
    if not isinstance(channels, list) or not all(isinstance(channel, str) for channel in channels):
        return False
    if "github" not in source:
        return True
    github = values.get("github")
    settings: JSON = github if isinstance(github, dict) else {}
    return (
        isinstance(source["github"], str)
        and REPOSITORY.fullmatch(source["github"]) is not None
        and isinstance(source.get("publisher"), str)
        and source["publisher"] in (values.get("approved_publishers") or {})
        and isinstance(source.get("private", False), bool)
        and (not source.get("private") or bool(settings.get("token_file")))
    )


def _validate_optional(values: JSON) -> None:
    """Shape of the optional keys at load time (#246); existence on disk is the preflight's job."""
    backup = values.get("backup")
    if backup is not None:
        if (
            not isinstance(backup, dict)
            or set(backup) - {"mode", "acknowledge_continuous_backup", "restore_point"}
            or backup.get("mode", "pg_dump") not in ("pg_dump", "external")
            or not isinstance(backup.get("restore_point", True), bool)
        ):
            raise AgentError("invalid_backup_config")
        if backup.get("mode") == "external" and backup.get("acknowledge_continuous_backup") is not True:
            raise AgentError("external_backup_not_acknowledged")  # The operator owns the PITR (#683).
    if "drain_marker" in values and not _absolute(values["drain_marker"]):
        raise AgentError("invalid_drain_marker")  # The rolling update's marker (#682).
    grace = values.get("drain_grace", 20)
    if isinstance(grace, bool) or not isinstance(grace, int | float) or not 0 <= grace <= 600:
        raise AgentError("invalid_drain_grace")
    paths = values.get("recovery_paths")
    if paths is not None and (not isinstance(paths, list) or not all(_absolute(path) for path in paths)):
        raise AgentError("invalid_recovery_path")
    if "project_requirements_lock" in values and not _absolute(values["project_requirements_lock"]):
        raise AgentError("invalid_config_path")  # The derived project's own pins (#495), like PATH_KEYS.
    if "smoke_token_file" in values and not _absolute(values["smoke_token_file"]):
        raise AgentError("invalid_smoke_token_path")
    for key in TOOL_SETTINGS:
        if key in values and not _absolute(values[key]):
            raise AgentError("invalid_tool_path")
    if "socket_gid" in values and (type(values["socket_gid"]) is not int or values["socket_gid"] < 0):
        raise AgentError("invalid_socket_gid")
    status = values.get("status_http")
    if status is not None:
        url = urlsplit(status.get("public_url", "")) if isinstance(status, dict) else None
        port = status.get("port") if isinstance(status, dict) else None
        if (
            url is None
            or type(port) is not int
            or not 1 <= port <= 65535
            or url.scheme not in ("http", "https")
            or not url.hostname
            or status.get("host", "127.0.0.1") not in ("127.0.0.1", "::1")
        ):
            raise AgentError("insecure_status_listener")
    sources = values.get("catalogue_sources")
    if sources is not None and (
        not isinstance(sources, list)
        or not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in sources)
        or not all(_valid_source(item, values) for item in sources)
    ):
        raise AgentError("invalid_catalogue_sources")
    if "github" in values and not github_settings_valid(values["github"]):
        raise AgentError("invalid_github_settings")
    if "allow_version_jumps" in values and not isinstance(values["allow_version_jumps"], bool):
        raise AgentError("invalid_boolean_setting")  # Emergency switch for skipping core lines (#493).
    interval = values.get("drift_check_interval")
    if interval is not None and (isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 0):
        raise AgentError("invalid_numeric_setting")  # Seconds; 0 turns the inventory cache off (#281).
    interval = values.get("update_check_interval")
    if interval is not None and (isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 0):
        raise AgentError("invalid_numeric_setting")  # Seconds between unattended checks; 0 turns them off (#501).
    for key in NUMERIC_SETTINGS:
        if key in values:
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise AgentError("invalid_numeric_setting")
    control = values.get("service_control")
    if control is not None and (
        not isinstance(control, dict)
        or set(control) - {"control_root", "state_root", "timeout"}
        or not _absolute(control.get("control_root"))
        or not _absolute(control.get("state_root"))
        or control["control_root"] == control["state_root"]
        or (
            "timeout" in control
            and (isinstance(control["timeout"], bool) or not isinstance(control["timeout"], (int, float)))
        )
        or control.get("timeout", 1) <= 0
    ):
        raise AgentError("invalid_service_control")  # Containers without the Docker socket (#651).
    # Self-update (#497): the agent's own versions live under agent_root.
    if "agent_root" in values and (not _absolute(values["agent_root"]) or not Path(values["agent_root"]).is_dir()):
        raise AgentError("invalid_agent_root")
    publishers = values.get("agent_publishers")
    if publishers is not None and (
        not isinstance(publishers, list)
        or not all(isinstance(item, str) and item in (values.get("approved_publishers") or {}) for item in publishers)
    ):
        raise AgentError("invalid_agent_publishers")


def read_environment(path: StrPath) -> dict[str, str]:
    result = {}
    for line in private_file(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AgentError("invalid_environment_file")
        key, value = line.split("=", 1)
        if not ENV_KEY.fullmatch(key):
            raise AgentError("invalid_environment_key")
        # No shell expansion, sourcing, substitutions or inherited owner credentials.
        words = shlex.split(value, comments=False)
        if len(words) > 1:
            raise AgentError("invalid_environment_value")
        result[key] = words[0] if words else ""
    return result


PATH_KEYS = (
    "storage_root",
    "releases_root",
    "project_root",
    "active_link",
    "socket_path",
    "token_file",
    "runtime_env_file",
    "migration_env_file",
    "build_env_file",
    "requirements_lock",
    "wheelhouse",
    "maintenance_marker",
)
REQUIRED_KEYS = (*PATH_KEYS, "project_paths", "approved_publishers", "commands", "health_urls", "backend")
RESERVED_PROJECT_PATHS = (
    "skeleton",
    ".venv",
    "deployment_agent",
    "staticfiles",
    "archives",
    "retained",
    "inventory.json",
    "owned-files.json",
    "wheels",
)
COMPOSE_NAME = r"[a-zA-Z0-9_-]+"


def _validate_paths(values: JSON) -> None:
    for key in PATH_KEYS:
        if not isinstance(values[key], str) or not Path(values[key]).is_absolute():
            raise AgentError("invalid_config_path")
    if values["backend"] not in ("directory", "compose"):
        raise AgentError("invalid_backend")
    if not isinstance(values["approved_publishers"], dict) or not values["approved_publishers"]:
        raise AgentError("missing_publishers")


def _validate_project_paths(values: JSON) -> None:
    if not isinstance(values["project_paths"], list) or not values["project_paths"]:
        raise AgentError("missing_project_paths")
    for value in values["project_paths"]:
        if (
            not isinstance(value, str)
            or value in ("", ".")
            or value.startswith("/")
            or ".." in Path(value).parts
            or Path(value).parts[0] in RESERVED_PROJECT_PATHS
        ):
            raise AgentError("invalid_project_path")


def _validate_commands(values: JSON) -> None:
    commands = values["commands"]
    if not isinstance(commands, dict) or any(name not in commands for name in ("drain", "restart", "stop")):
        raise AgentError("missing_lifecycle_commands")
    if ("worker_stop" in commands) != ("worker_restart" in commands):
        raise AgentError("incomplete_worker_commands")
    for command in commands.values():
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(argument, str) and argument and "\x00" not in argument for argument in command)
            or not Path(command[0]).is_absolute()
        ):
            raise AgentError("invalid_fixed_command")


def _validate_health(values: JSON) -> None:
    if not isinstance(values["health_urls"], list) or not values["health_urls"]:
        raise AgentError("missing_health_checks")
    for url in values["health_urls"]:
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname or urlsplit(url).username:
            raise AgentError("invalid_health_url")


def _validate_compose(values: JSON) -> None:
    if values["backend"] != "compose":
        return
    compose = values.get("compose", {})
    for key in ("file", "env_file", "dockerfile"):
        if not Path(compose.get(key, "")).is_absolute():
            raise AgentError("invalid_compose_config")
    for key in ("project", "web_service", "migrator_service"):
        if not re.fullmatch(COMPOSE_NAME, compose.get(key, "")):
            raise AgentError("invalid_compose_service")
    workers = compose.get("worker_services", [])
    if (
        not isinstance(workers, list)
        or len(set(workers)) != len(workers)
        or not all(isinstance(name, str) and re.fullmatch(COMPOSE_NAME, name) for name in workers)
        or any(name in (compose.get("web_service"), compose.get("migrator_service")) for name in workers)
    ):
        raise AgentError("invalid_compose_worker_services")
    if not isinstance(compose.get("base_image"), str) or not compose["base_image"]:
        raise AgentError("missing_compose_base_image")
    target = compose.get("target_environment")
    if not isinstance(target, dict) or not all(
        isinstance(target.get(key), str) and target[key]
        for key in ("python_full_version", "python_version", "platform_machine", "sys_platform")
    ):
        raise AgentError("compose_target_environment_required")


def _validate_credentials(values: JSON) -> None:
    """The owner DSN never reaches the runtime or build profiles; both are PostgreSQL."""
    runtime = read_environment(values["runtime_env_file"])
    migration = read_environment(values["migration_env_file"])
    build = read_environment(values["build_env_file"])
    if "MIGRATION_DATABASE_URL" in runtime or "MIGRATION_DATABASE_URL" in build:
        raise AgentError("migration_secret_in_runtime")
    if (values.get("cluster") or {}).get("role") == "follower":
        # A follower never migrates (#678): the owner credential never reaches its node.
        if migration.get("MIGRATION_DATABASE_URL") or values.get("backup_env_file"):
            raise AgentError("follower_has_owner")
        if not effective_runtime_database(runtime):
            raise AgentError("missing_runtime_credentials")
        return
    if not migration.get("MIGRATION_DATABASE_URL"):
        raise AgentError("missing_migration_credentials")
    runtime_url = effective_runtime_database(runtime)
    if migration.get("MIGRATION_DATABASE_URL") == runtime_url:
        raise AgentError("shared_database_credentials")
    if not runtime_url:
        raise AgentError("missing_runtime_credentials")
    for url in (runtime_url, migration["MIGRATION_DATABASE_URL"]):
        if urlsplit(url).scheme not in ("postgres", "postgresql"):
            raise AgentError("unsupported_database")  # PostgreSQL only (#144).
    if values.get("backup_env_file"):
        if not isinstance(values["backup_env_file"], str) or not Path(values["backup_env_file"]).is_absolute():
            raise AgentError("invalid_backup_environment_path")
        backup = urlsplit(read_environment(values["backup_env_file"]).get("MIGRATION_DATABASE_URL", ""))
        owner = urlsplit(migration["MIGRATION_DATABASE_URL"])
        if (
            backup.scheme not in ("postgres", "postgresql")
            or owner.scheme not in ("postgres", "postgresql")
            or backup.path != owner.path
            or backup.username != owner.username
        ):
            raise AgentError("backup_database_identity_mismatch")


@dataclass(frozen=True)
class Config:
    values: JSON
    path: Path | None = None  # The loaded file, for the selfcheck of a new agent version (#497).

    @classmethod
    def load(cls, path: StrPath) -> Self:
        try:
            values = json.loads(private_file(path).read_bytes())
        except (OSError, ValueError):
            raise AgentError("invalid_agent_config") from None
        if not isinstance(values, dict) or any(key not in values for key in REQUIRED_KEYS):
            raise AgentError("missing_agent_config")
        # The order of the checks is part of the contract: a configuration with two problems keeps
        # reporting the same first error (#317).
        _validate_paths(values)
        _validate_project_paths(values)
        _validate_commands(values)
        _validate_health(values)
        _validate_optional(values)
        _validate_compose(values)
        validate_cluster(values)
        _validate_credentials(values)
        token = private_file(values["token_file"]).read_text().strip()
        if not token.isascii() or len(token) < 32:
            raise AgentError("invalid_agent_token")
        return cls(values, Path(path))

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def environment(self, profile: str, release: StrPath) -> dict[str, str]:
        result: dict[str, str] = {
            "PATH": self.get("operator_path", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HOME": self["storage_root"],
        }
        result.update(read_environment(self[f"{profile}_env_file"]))
        if profile != "migration":
            result.pop("MIGRATION_DATABASE_URL", None)
        release = Path(release)
        result["SKELETON_PROJECT_ROOT"] = str(release)
        result["SKELETON_DEPLOYMENT_INVENTORY"] = str(release / "inventory.json")
        result["SKELETON_MAINTENANCE_MARKER"] = self["maintenance_marker"]
        result["STATIC_ROOT"] = str(release / "staticfiles")
        result["SKELETON_STATIC_ROOT"] = str(release / "staticfiles")
        result["PYTHONPATH"] = str(release)
        if profile == "migration":
            result["PYTHONPATH"] += os.pathsep + str(release / "archives")
        return result
