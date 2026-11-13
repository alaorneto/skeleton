"""Offline adoption of an existing project as generation 0 (#317)."""

import io
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING
from zipfile import ZipFile

from skeleton.system.packages import validate_manifest

from .releases import (
    member_files,
    owned_snapshot,
    verified,
)
from .storage import (
    AgentError,
    atomic_json,
    deployment_lock,
    inventory_files,
    load_json,
)
from .types import JSON, StrPath

if TYPE_CHECKING:
    from .service import DeploymentService


def initialize(
    service: "DeploymentService",
    inventory_path: StrPath,
    *,
    core_package: StrPath,
    app_packages: Iterable[StrPath] = (),
) -> JSON:
    """Offline adoption: signed base and an explicit project inventory, never guess a fork."""
    with deployment_lock(service.store.root):
        if (service.store.root / "active.json").exists():
            raise AgentError("already_initialized", 409)
        inventory = load_json(inventory_path)
        if not isinstance(inventory, dict) or not isinstance(inventory.get("apps", []), list):
            raise AgentError("invalid_initial_inventory")
        core = service.upload(Path(core_package).read_bytes())
        if core["manifest"]["kind"] != "core":
            raise AgentError("core_artifact_required")
        for package in app_packages:
            service.upload(Path(package).read_bytes())
        source = Path(service.config["project_root"]).resolve()
        _adopt_active_link(service, source)
        files = _core_files(service, core)
        origin = _core_origin(service, source, core, files)
        entries = _adopted_apps(service, source, inventory)
        inventory = {
            "generation": 0,
            "core": {
                "manifest": core["manifest"],
                "version": core["manifest"]["version"],
                "origin": origin,
                "digest": core["digest"],
                "publisher": core["manifest"]["publisher"],
                "files": files,
            },
            "apps": entries,
            "retained_schema_paths": [],
            "migration_archives": [],
        }
        atomic_json(source / "inventory.json", inventory, 0o644)
        snapshot = owned_snapshot(source)
        service._write_active(source, inventory, snapshot)
        return inventory


def _adopt_active_link(service: "DeploymentService", source: Path) -> None:
    link = Path(service.config["active_link"])
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != source:
            raise AgentError("initial_active_pointer_conflict", 409)
    else:
        link.symlink_to(source, target_is_directory=True)


def _core_files(service: "DeploymentService", core: JSON) -> JSON:
    """Files of the signed core wheel: a checkout is matched against this base."""
    core_verified = verified(service.store, service.config, core["digest"])
    with ZipFile(core_verified.path) as payload:
        dependency = next(
            item
            for item in core["manifest"]["python_dependencies"]
            if item["name"].replace("_", "-").lower() == "django-skeleton"
        )
        with ZipFile(io.BytesIO(payload.read(dependency["wheel"]))) as wheel:
            return member_files(wheel)


def _core_origin(service: "DeploymentService", source: Path, core: JSON, files: JSON) -> str:
    """``source`` or ``installed``; a changed or unknown core is not adoptable."""
    if (source / "skeleton").is_dir():
        actual = {f"skeleton/{path}": row for path, row in inventory_files(source / "skeleton").items()}
        if set(actual) != set(files) or any(actual[path]["sha256"] != row["sha256"] for path, row in files.items()):
            raise AgentError("core_fork_or_unknown_base", 409)
        return "source"
    result_path = service.store.root / "probe" / "adopt-core.json"
    script = "import importlib.metadata,json,pathlib,skeleton,sys; root=pathlib.Path(skeleton.__file__).parent; files={}; import hashlib; "
    script += "files={'skeleton/'+str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}; "
    script += "pathlib.Path(sys.argv[1]).write_text(json.dumps({'version':importlib.metadata.version('django-skeleton'),'files':files}))"
    service.backend.python(
        source,
        ["-c", script, result_path],
        "export" if service.config["backend"] == "compose" else "runtime",
        code="core_adoption_failed",
    )
    actual = load_json(result_path)
    result_path.unlink(missing_ok=True)
    if actual["version"] != core["manifest"]["version"] or actual["files"] != {
        key: row["sha256"] for key, row in files.items()
    }:
        raise AgentError("core_fork_or_unknown_base", 409)
    return "installed"


def _adopted_apps(service: "DeploymentService", source: Path, inventory: JSON) -> list[JSON]:
    entries = []
    identities: dict[str, set[str]] = {"app_id": set(), "app_label": set(), "package_name": set()}
    for entry in inventory.get("apps", []):
        manifest = validate_manifest(entry["manifest"])
        if manifest["kind"] != "app":
            raise AgentError("initial_app_artifact_required")
        for key, seen in identities.items():
            if manifest[key] in seen:
                raise AgentError("duplicate_initial_component", 409)
            seen.add(manifest[key])
        state = entry.get("state", "project-local")
        if state not in ("installed", "project-local", "schema_retained"):
            raise AgentError("invalid_component_state")
        if state == "schema_retained":
            raise AgentError("adopt_retained_requires_existing_journal")
        if state == "installed":
            path = _installed_app_path(service, source, entry, manifest)
        else:
            path = _local_app_path(source, entry, manifest)
        entries.append(
            {
                **entry,
                "manifest": manifest,
                "state": state,
                "path": str(path),
                "origin": "project-local" if state == "project-local" else "installed",
            }
        )
    return entries


def _installed_app_path(service: "DeploymentService", source: Path, entry: JSON, manifest: JSON) -> Path:
    """An installed app's code must be exactly its signed package."""
    package = verified(service.store, service.config, entry["digest"])
    if package.manifest != manifest:
        raise AgentError("initial_app_artifact_conflict")
    path = source / "applications" / manifest["package_name"]
    actual_files = inventory_files(path)
    expected_files = {
        record["path"].removeprefix("app/"): record for record in manifest["files"] if record["path"].startswith("app/")
    }
    if set(actual_files) != set(expected_files) or any(
        actual_files[name]["sha256"] != record["sha256"]
        or actual_files[name].get("size") != record["size"]
        or actual_files[name]["type"] != "file"
        for name, record in expected_files.items()
    ):
        raise AgentError("initial_app_local_drift", 409)
    return path


def _local_app_path(source: Path, entry: JSON, manifest: JSON) -> Path:
    path = Path(entry.get("path", source / "applications" / manifest["package_name"]))
    if not path.is_absolute():
        path = source / path
    if source not in path.resolve().parents:
        raise AgentError("initial_local_app_outside_project", 409)
    if not path.is_dir() or path.is_symlink():
        raise AgentError("initial_local_app_missing", 409)
    return path.resolve()
