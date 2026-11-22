"""First start of the agent: install the core from a signed release package, without adoption (#652).

Adoption (``initialize``) compares a core the operator installed by hand with a signed package.
A container deployment has no such core: on its first start the agent installs a **published**
release itself. ``install_initial`` takes the release package from a local file or from a
catalogue source (the latest version, or the one pinned), verifies it against
``approved_publishers`` before anything is extracted, builds generation 0 from ``project_root``
(the project files of the image) and the package's core, lock and wheels, applies the migrations
and the table catalog with the owner, checks the schema, mirrors the inventory into the database
and only then switches the active pointer. Nothing is built from a checkout.

Before the setup the instance has no runtime configuration yet, so the schema check of this step
runs with the migration profile, as the setup's own migration does; the setup's schema step then
confirms it with the runtime role. An interrupted first start leaves no active pointer: the next
start removes the partial release and starts again (migrations are idempotent).

``container_config`` turns the bootstrap variables of the agent container into the agent's private
configuration (``agent.json``, the three environment files and the control token), written once.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packaging.version import InvalidVersion, Version

from .releases import (
    _core_from_package,
    _finalize,
    _install_core_wheels,
    copy_project,
    freeze,
    owned_snapshot,
    verified,
)
from .storage import AgentError, atomic_json, deployment_lock, file_hash
from .types import JSON

if TYPE_CHECKING:
    from .service import DeploymentService

__all__ = ["BOOTSTRAP_PREFIX", "container_config", "install_initial", "write_container_config"]

BOOTSTRAP_PREFIX = "0-bootstrap"
# The official publisher and source (#656): a fork sets the three variables instead.
OFFICIAL_PUBLISHER = "skeleton"
OFFICIAL_PUBLISHER_KEY = "/opt/agent/publishers/skeleton.pub.pem"
OFFICIAL_SOURCE = "alaorneto/skeleton"
CORE = "django-skeleton"
FEATURE_VARIABLES = ("SKELETON_BUNDLED_APPS", "SKELETON_ENABLE_EXAMPLE_APP", "SKELETON_BILLING_ENABLED")


# --------------------------------------------------------------------------- configuration


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value or "\n" in value or "\x00" in value:
        raise AgentError("missing_bootstrap_variable", details={"variable": name})
    return value


def _env_line(name: str, value: str) -> str:
    if any(char in value for char in "\n\r\x00\"'\\ $`"):
        raise AgentError("invalid_bootstrap_value", details={"variable": name})
    return f"{name}={value}\n"


def container_config(environ: Mapping[str, str]) -> tuple[JSON, dict[str, str]]:
    """``(agent.json values, {file name: content})`` of the agent container, from its variables.

    Directories (defaults are the paths of the Compose files): ``SKELETON_AGENT_PRIVATE``
    (``/agent/private``), ``SKELETON_AGENT_STATE`` (``/agent/state``), ``SKELETON_RELEASES``
    (``/releases``), ``SKELETON_CONTROL`` (``/control``), ``SKELETON_CONTROL_STATE``
    (``/control-state``) and ``SKELETON_AGENT_PROJECT`` (``/opt/project``). Required:
    ``SKELETON_RUNTIME_DATABASE_URL`` and ``SKELETON_MIGRATION_DATABASE_URL``. The publisher is the
    official one unless a fork sets it (#656): ``SKELETON_AGENT_PUBLISHER`` (``skeleton``),
    ``SKELETON_AGENT_PUBLISHER_KEY`` (the PEM versioned in ``deploy/release/publishers/``) and
    ``SKELETON_AGENT_SOURCE`` (``alaorneto/skeleton`` on GitHub, pinned to that publisher;
    ``none`` for no source). Optional:
    ``SKELETON_HEALTH_URL`` (``http://web:8000``), ``SKELETON_PUBLIC_HOST`` (the Host of the
    health checks), the instance paths and the feature switches, passed to every profile.
    """
    private = Path(environ.get("SKELETON_AGENT_PRIVATE", "/agent/private"))
    state = Path(environ.get("SKELETON_AGENT_STATE", "/agent/state"))
    releases = Path(environ.get("SKELETON_RELEASES", "/releases"))
    control = Path(environ.get("SKELETON_CONTROL", "/control"))
    control_state = Path(environ.get("SKELETON_CONTROL_STATE", "/control-state"))
    project = Path(environ.get("SKELETON_AGENT_PROJECT", "/opt/project"))
    for path in (private, state, releases, control, control_state, project):
        if not path.is_absolute():
            raise AgentError("invalid_bootstrap_path")
    runtime_url = _required(environ, "SKELETON_RUNTIME_DATABASE_URL")
    owner_url = _required(environ, "SKELETON_MIGRATION_DATABASE_URL")
    publisher = environ.get("SKELETON_AGENT_PUBLISHER") or OFFICIAL_PUBLISHER
    publisher_key = environ.get("SKELETON_AGENT_PUBLISHER_KEY") or OFFICIAL_PUBLISHER_KEY
    for name, value in (("SKELETON_AGENT_PUBLISHER", publisher), ("SKELETON_AGENT_PUBLISHER_KEY", publisher_key)):
        _required({name: value}, name)
    if not Path(publisher_key).is_absolute():
        raise AgentError("invalid_bootstrap_path")
    features = "".join(_env_line(name, environ[name]) for name in FEATURE_VARIABLES if environ.get(name))
    instance = "".join(
        _env_line(name, environ[name])
        for name in ("SKELETON_INSTANCE_DB", "SKELETON_INSTANCE_KEY_FILE")
        if environ.get(name)
    )
    files = {
        "runtime.env": _env_line("DJANGO_SETTINGS_MODULE", "config.settings.production")
        + _env_line("DATABASE_URL", runtime_url)
        + instance
        + features,
        "migration.env": _env_line("DJANGO_SETTINGS_MODULE", "config.settings.migrate")
        + _env_line("MIGRATION_DATABASE_URL", owner_url)
        + features,
        "build.env": _env_line("DJANGO_SETTINGS_MODULE", "config.settings.build") + features,
    }
    agent = [sys.executable, "-m", "deployment_agent", "--config", str(private / "agent.json")]
    project_paths = environ.get("SKELETON_AGENT_PROJECT_PATHS", "manage.py,config,applications,examples").split(",")
    health = environ.get("SKELETON_HEALTH_URL", "http://web:8000").rstrip("/")
    values: JSON = {
        "backend": "directory",
        "storage_root": str(state),
        "releases_root": str(releases),
        "project_root": str(project),
        "active_link": str(releases / "current"),
        "socket_path": str(control / "agent.sock"),
        "token_file": str(control / "agent.token"),
        "runtime_env_file": str(private / "runtime.env"),
        "migration_env_file": str(private / "migration.env"),
        "build_env_file": str(private / "build.env"),
        "requirements_lock": str(private / "project-requirements.lock"),
        "wheelhouse": str(private / "wheelhouse"),
        "maintenance_marker": str(control / "maintenance"),
        "project_paths": [item for item in project_paths if item],
        "approved_publishers": {publisher: publisher_key},
        "service_control": {"control_root": str(control / "services"), "state_root": str(control_state)},
        "commands": {
            "drain": [*agent, "control", "stop", "web"],
            "stop": [*agent, "control", "stop", "web"],
            "restart": [*agent, "control", "restart", "web", "--release", "{release}"],
            "worker_stop": [*agent, "control", "stop", "worker", "scheduler"],
            "worker_restart": [*agent, "control", "restart", "worker", "scheduler", "--release", "{release}"],
        },
        "health_urls": [f"{health}/health/live/", f"{health}/health/ready/"],
        "python": sys.executable,
        "pg_dump": environ.get("SKELETON_PG_DUMP", "/usr/bin/pg_dump"),
        "pg_restore": environ.get("SKELETON_PG_RESTORE", "/usr/bin/pg_restore"),
        "psql": environ.get("SKELETON_PSQL", "/usr/bin/psql"),
        "recovery_paths": [],
    }
    if environ.get("SKELETON_PUBLIC_HOST"):
        values["health_host"] = environ["SKELETON_PUBLIC_HOST"]
    source = environ.get("SKELETON_AGENT_SOURCE", OFFICIAL_SOURCE)
    if source and source.lower() != "none":  # "none": no catalogue source, only SKELETON_AGENT_RELEASE_FILE.
        values["catalogue_sources"] = [{"id": "official", "github": source, "publisher": publisher}]
    return values, files


def write_container_config(environ: Mapping[str, str]) -> Path:
    """Write the private configuration once; an existing ``agent.json`` is kept as it is."""
    import json

    values, files = container_config(environ)
    private = Path(values["runtime_env_file"]).parent
    config_path = private / "agent.json"
    for directory in (private, Path(values["storage_root"])):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    for directory in (Path(values["releases_root"]), Path(values["service_control"]["control_root"])):
        directory.mkdir(parents=True, exist_ok=True)
    token = Path(values["token_file"])
    if not token.exists():
        token.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        try:
            os.write(descriptor, secrets.token_hex(32).encode("ascii"))
        finally:
            os.close(descriptor)
    if config_path.exists():
        return config_path
    for name, content in files.items():
        target = private / name
        target.write_text(content, encoding="utf-8")
        os.chmod(target, 0o600)
    lock = Path(values["requirements_lock"])
    if not lock.exists():
        lock.write_text("", encoding="utf-8")  # The publisher's lock covers the core; no project pins.
        os.chmod(lock, 0o600)
    Path(values["wheelhouse"]).mkdir(exist_ok=True)
    temporary = private / ".agent.json.new"
    temporary.write_text(json.dumps(values, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, config_path)
    return config_path


# --------------------------------------------------------------------------- the first installation


def _latest_release(service: DeploymentService, version: str | None, source_id: str | None) -> tuple[str, str]:
    """``(version, source id)`` of the release to install: the pinned one, else the newest."""
    from .catalogue import cached_entries

    service.check_updates()
    candidates: list[tuple[Version, str, str]] = []
    for source in service.catalogue_sources():
        if source_id and source["id"] != source_id:
            continue
        for entry in cached_entries(service, source) or []:
            if entry.get("component_id") != CORE or entry.get("kind") != "core":
                continue
            try:
                parsed = Version(entry["version"])
            except (InvalidVersion, KeyError, TypeError):
                continue
            if version and entry["version"] != version:
                continue
            candidates.append((parsed, entry["version"], source["id"]))
    if not candidates:
        raise AgentError("initial_release_unavailable", 409)
    _parsed, chosen, chosen_source = max(candidates)
    return chosen, chosen_source


def _release_package(
    service: DeploymentService, *, release_file: str | None, version: str | None, source_id: str | None
) -> JSON:
    """The uploaded (verified) release package with a target for this server; else refused."""
    if release_file:
        result = service.upload(Path(release_file).read_bytes())
    else:
        chosen, source = _latest_release(service, version, source_id)
        result = service.fetch_release(CORE, chosen, source)
    if result.get("kind") != "release":
        raise AgentError("release_package_required", 409)
    if not result.get("target"):
        raise AgentError("release_target_unavailable", 409)
    if version and result["manifest"]["version"] != version:
        raise AgentError("initial_release_version_conflict", 409)
    return result


def _discard_partial(releases_root: Path) -> None:
    """A first start that stopped half-way left a staging release: never reused, always removed."""
    for path in releases_root.glob(f"{BOOTSTRAP_PREFIX}-*"):
        if path.is_dir() and not path.is_symlink():
            for item in path.rglob("*"):
                if item.is_dir() and not item.is_symlink():
                    os.chmod(item, 0o700)
            shutil.rmtree(path)


def _build(service: DeploymentService, core_digest: str, release: Path) -> JSON:
    config, store, backend = service.config, service.store, service.backend
    release.mkdir(parents=True, exist_ok=False)
    copy_project(config, config["project_root"], release, managed_packages=set())
    core = _core_from_package(verified(store, config, core_digest).manifest, {}, core_digest)
    environment = backend.target_environment()
    _install_core_wheels(config, store, release, {}, core, {CORE})
    return _finalize(config, store, backend, release, {"generation": -1}, core, {}, [], environment, [], [])


def install_initial(
    service: DeploymentService,
    *,
    release_file: str | None = None,
    version: str | None = None,
    source_id: str | None = None,
) -> JSON:
    """Install generation 0 from a signed release package; with an active release, change nothing."""
    from uuid import uuid4

    with deployment_lock(service.store.root):
        if (service.store.root / "active.json").exists():
            return {"state": "installed", "generation": service.state()["inventory"]["generation"]}
        active = Path(service.config["active_link"])
        if active.exists() or active.is_symlink():
            raise AgentError("initial_active_pointer_conflict", 409)
        releases_root = Path(service.config["releases_root"])
        releases_root.mkdir(parents=True, exist_ok=True)
        _discard_partial(releases_root)
        package = _release_package(service, release_file=release_file, version=version, source_id=source_id)
        release = releases_root / f"{BOOTSTRAP_PREFIX}-{uuid4().hex}"
        backend = service.backend
        with backend.logging_to(release.name):
            inventory = _build(service, package["core"]["digest"], release)
            infrastructure = release / "infrastructure.json"
            backend.export_infrastructure(release, infrastructure)
            inventory["frozen_infrastructure"] = {"path": str(infrastructure), "sha256": file_hash(infrastructure)}
            atomic_json(release / "inventory.json", inventory, 0o644)
            backend.manage(release, ["migrate", "--noinput"], "migration", code="migration_failed")
            backend.manage(release, ["sync_table_catalog"], "migration", code="catalogue_sync_failed")
            backend.manage(release, ["check_skeleton_schema"], "migration", code="schema_check_failed")
            backend.manage(
                release,
                ["sync_deployment_state", "--inventory", release / "inventory.json"],
                "migration",
                code="state_sync_failed",
            )
            owned = owned_snapshot(release)
            atomic_json(release / "owned-files.json", owned, 0o644)
            freeze(release)
        backend.cutover(release)
        service._write_active(release, inventory, owned)
        return {
            "state": "installed",
            "generation": inventory["generation"],
            "core": {key: inventory["core"][key] for key in ("version", "digest", "origin", "publisher")},
            "release_package": package["digest"],
        }


def container_main(environ: Mapping[str, Any] | None = None) -> int:
    """Entrypoint of the agent container: configuration, first installation, then ``serve``."""
    import json

    from .config import Config
    from .server import serve
    from .service import DeploymentService

    environ = os.environ if environ is None else environ
    try:
        config = Config.load(write_container_config(environ))
        service = DeploymentService(config)
        result = install_initial(
            service,
            release_file=environ.get("SKELETON_AGENT_RELEASE_FILE") or None,
            version=environ.get("SKELETON_CORE_VERSION") or None,
            source_id=None,
        )
        print(json.dumps({"event": "agent.bootstrap", **result}, sort_keys=True), file=sys.stderr, flush=True)
    except AgentError as error:
        print(json.dumps({"event": "agent.bootstrap_failed", "error": error.code}), file=sys.stderr, flush=True)
        return 1
    return serve(service)


if __name__ == "__main__":
    raise SystemExit(container_main())
