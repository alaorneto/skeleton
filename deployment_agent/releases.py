"""Verified artifact composition, hash-locked environments and exact ownership diffs."""

import hashlib
import os
import re
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from email.parser import BytesParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zipfile import ZipFile

from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from skeleton.system.dependencies import dependents_of, resolve_dependencies
from skeleton.system.packages import (
    PackageError,
    extract_package,
    validate_package,
    validate_wheel_set,
)

from .config import installed_bundled_app_ids
from .storage import AgentError, atomic_bytes, atomic_json, file_hash, inventory_files, load_json
from .types import JSON, StrPath

if TYPE_CHECKING:
    from skeleton.system.dependencies import Resolution
    from skeleton.system.packages import VerifiedPackage

    from .backends import Backend
    from .config import Config
    from .storage import Store


def verified(store: "Store", config: "Config", artifact: str) -> "VerifiedPackage":
    try:
        return validate_package(store.path("uploads", artifact), config["approved_publishers"])
    except PackageError:
        raise AgentError("package_verification_failed", 409) from None


def member_files(archive: ZipFile) -> JSON:
    """Hash and size of every core file of an open wheel, each member read once (#317)."""
    result = {}
    for name in archive.namelist():
        if name.startswith("skeleton/") and not name.endswith("/"):
            data = archive.read(name)
            result[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    return result


def wheel_files(path: StrPath) -> JSON:
    with ZipFile(path) as archive:
        return member_files(archive)


def migrations(manifest: JSON) -> dict[str, str]:
    return {
        entry["path"]: entry["sha256"]
        for entry in manifest["files"]
        if entry["path"].startswith("app/migrations/") and entry["path"].endswith(".py")
    }


def compare_history(old: JSON, new: JSON) -> None:
    previous = migrations(old)
    current = migrations(new)
    if any(current.get(path) != value for path, value in previous.items()):
        raise AgentError("migration_history_modified", 409)


def parse_lock(path: StrPath, target_environment: Mapping[str, Any] | None = None) -> JSON:
    text = Path(path).read_text().replace("\\\n", " ")
    rows: JSON = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split(" #", 1)[0].strip()
        hashes = re.findall(r"--hash=sha256:([a-f0-9]{64})", line)
        requirement_text = re.sub(r"\s*--hash=sha256:[a-f0-9]{64}", "", line).strip()
        try:
            requirement = Requirement(requirement_text)
        except Exception:  # noqa: BLE001 - any parse failure of the lock is the same refusal
            raise AgentError("invalid_requirements_lock") from None
        pins = list(requirement.specifier)
        if len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version or requirement.url or not hashes:
            raise AgentError("unlocked_dependency")
        name = canonicalize_name(requirement.name)
        if requirement.marker and not requirement.marker.evaluate(
            cast(Mapping[str, Any], target_environment or default_environment())
        ):
            continue
        if name in rows:
            raise AgentError("duplicate_locked_dependency")
        rows[name] = {"version": pins[0].version, "hashes": set(hashes), "extras": set(requirement.extras)}
    return rows


def wheel_metadata(path: StrPath) -> tuple[str, str]:
    with ZipFile(path) as archive:
        entries = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(entries) != 1:
            raise AgentError("invalid_wheel")
        metadata = BytesParser().parsebytes(archive.read(entries[0]))
        return canonicalize_name(metadata["Name"]), metadata["Version"]


def _package_selections(manifests: Sequence[JSON], rows: JSON, target_environment: Mapping[str, Any]) -> JSON:
    """Exact pins the packages declare for the target; one version per distribution."""
    selections: JSON = {}
    for manifest in manifests:
        for dependency in manifest["python_dependencies"]:
            marker = dependency.get("marker")
            if marker:
                if not Marker(marker).evaluate(target_environment):
                    continue
            name = canonicalize_name(dependency["name"])
            version = dependency["version"]
            if name in selections and Version(selections[name]["version"]) != Version(version):
                raise AgentError("dependency_version_conflict", 409)
            record = selections.setdefault(
                name, {"version": version, "hashes": set(), "extras": rows.get(name, {}).get("extras", set())}
            )
            record["hashes"].add(dependency["sha256"])
    return selections


def build_lock(
    config: "Config", release: Path, manifests: Sequence[JSON], target_environment: Mapping[str, Any] | None = None
) -> JSON:
    target_environment = target_environment or default_environment()
    rows = parse_lock(config["requirements_lock"], target_environment)
    wheels = release / "wheels"
    rows.update(_package_selections(manifests, rows, target_environment))
    if "django-skeleton" not in rows or "django" not in rows:
        raise AgentError("missing_core_dependencies", 409)
    wheelhouse = Path(config["wheelhouse"])
    available: dict[str, set[str]] = {}
    for path in [*wheelhouse.glob("*.whl"), *wheels.glob("*.whl")]:
        name, version = wheel_metadata(path)
        value = file_hash(path)
        if name not in rows or Version(version) != Version(rows[name]["version"]) or value not in rows[name]["hashes"]:
            continue
        target = wheels / path.name
        if target.exists() and file_hash(target) != value:
            raise AgentError("wheel_filename_collision", 409)
        if not target.exists():
            shutil.copyfile(path, target)
        available.setdefault(name, set()).add(value)
    if any(name not in available for name in rows):
        raise AgentError("locked_wheel_unavailable", 409)
    for path in wheels.glob("*.whl"):
        name, version = wheel_metadata(path)
        if name not in available or file_hash(path) not in available[name]:
            path.unlink()
    try:
        ownership = validate_wheel_set(list(wheels.glob("*.whl")), target_environment=target_environment)
    except PackageError:
        raise AgentError("wheel_set_conflict", 409) from None
    atomic_json(release / "wheel-ownership.json", ownership, 0o644)
    lines = []
    for name, row in sorted(rows.items()):
        extras = f"[{','.join(sorted(row['extras']))}]" if row["extras"] else ""
        lines.append(
            f"{name}{extras}=={row['version']} "
            + " ".join(f"--hash=sha256:{value}" for value in sorted(available[name]))
        )
    atomic_bytes(release / "requirements.lock", ("\n".join(lines) + "\n").encode(), 0o644)
    return rows


def copy_project(config: "Config", source: StrPath, release: Path, *, managed_packages: set[str]) -> None:
    source = Path(source)
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".private", ".venv", "*.sqlite3", "node_modules")

    def ignore(directory: str, names: list[str]) -> set[str]:
        excluded = set(ignored(directory, names))
        if Path(directory) == source / "applications":
            excluded.update(managed_packages)
        return excluded

    for relative in config["project_paths"]:
        origin = Path(source) / relative
        if not origin.exists() or origin.is_symlink():
            raise AgentError("project_path_missing_or_link", 409)
        target = release / relative
        if origin.is_dir():
            if any(path.is_symlink() for path in origin.rglob("*")):
                raise AgentError("project_symlink", 409)
            shutil.copytree(origin, target, ignore=ignore)
            # The active release is frozen; only the new staging tree is writable.
            os.chmod(target, target.stat().st_mode | 0o200)
            for directory in target.rglob("*"):
                if directory.is_dir():
                    os.chmod(directory, directory.stat().st_mode | 0o200)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
    (release / "applications").mkdir(exist_ok=True)
    (release / "applications/__init__.py").touch(exist_ok=True)
    for name in ("archives", "retained", "wheels", "staticfiles"):
        (release / name).mkdir(exist_ok=True)


def owned_snapshot(release: StrPath) -> JSON:
    result = inventory_files(release)
    result.pop("owned-files.json", None)
    for path, row in result.items():
        if path.startswith("applications/"):
            row["owner"] = "app:" + path.split("/")[1]
        elif path.startswith(".venv/"):
            row["owner"] = "environment"
        elif path.startswith(("staticfiles/", "archives/", "retained/", "wheels/")) or path in (
            "inventory.json",
            "requirements.lock",
        ):
            row["owner"] = "deployment"
        else:
            row["owner"] = "project"
    return result


def check_drift(release: StrPath, expected: JSON | None = None) -> None:
    expected = expected or load_json(Path(release) / "owned-files.json")
    if owned_snapshot(release) != expected:
        raise AgentError("local_file_drift", 409)


def file_diff(before: JSON, after: JSON) -> list[JSON]:
    result = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        result.append(
            {
                "path": path,
                "owner": cast(JSON, new or old).get("owner", "deployment"),
                "action": "add" if old is None else "remove" if new is None else "change",
                "before": old.get("sha256") if old else None,
                "after": new.get("sha256") if new else None,
            }
        )
    return result


def freeze(release: StrPath) -> None:
    for path in Path(release).rglob("*"):
        if not path.is_symlink():
            mode = path.stat().st_mode
            os.chmod(path, 0o555 if path.is_dir() or mode & 0o111 else 0o444)
    os.chmod(release, 0o555)


# Problems an operator fixes by changing the plan, as opposed to the target runtime.
DEPENDENCY_CODES = frozenset(
    {"missing_dependency", "incompatible_dependency_version", "dependency_cycle", "conflicting_release"}
)
SUGGESTIBLE_CODES = frozenset({"missing_dependency", "incompatible_dependency_version"})
MAX_PROBLEM_DETAILS = 64


def target_django_version(config: "Config", manifests: Sequence[JSON], environment: Mapping[str, Any]) -> str:
    """The Django version build_lock will select: the locked pin, overridden by active manifest pins."""
    version = parse_lock(config["requirements_lock"], environment).get("django", {}).get("version")
    for manifest in manifests:
        for dependency in manifest["python_dependencies"]:
            marker = dependency.get("marker")
            if marker and not Marker(marker).evaluate(environment):
                continue
            if canonicalize_name(dependency["name"]) == "django":
                version = dependency["version"]
    if version is None:
        raise AgentError("missing_core_dependencies", 409)
    return version


def dependency_error(resolution: "Resolution", suggest: Callable[..., Any] | None = None) -> AgentError:
    details = [problem.as_dict() for problem in resolution.problems[:MAX_PROBLEM_DETAILS]]
    if suggest is not None:
        for detail in details:
            if detail["code"] in SUGGESTIBLE_CODES:
                detail["suggestions"] = suggest(detail["dependency"], detail["required"])
    if any(problem.code in DEPENDENCY_CODES for problem in resolution.problems):
        return AgentError("dependency_unsatisfied", 409, details=details)
    return AgentError("target_runtime_incompatible", 409, details=details)


def resolve_target(
    manifests: Sequence[JSON], *, core: str, environment: Mapping[str, Any], django: str, provided: Iterable[str] = ()
) -> "Resolution":
    try:
        return resolve_dependencies(
            manifests,
            core=core,
            python=environment["python_full_version"],
            django=django,
            target_environment=environment,
            provided=provided,
        )
    except PackageError:
        raise AgentError("target_runtime_incompatible", 409) from None


def check_plan_dependencies(
    config: "Config",
    backend: "Backend",
    inventory: JSON,
    artifact_manifests: Sequence[JSON],
    removals: Sequence[str],
    suggest: Callable[..., Any] | None = None,
) -> "Resolution | None":
    """Resolve the resulting component set before any copy, extraction or lock work.

    suggest(dependency, required, target) returns catalogue releases for a missing or
    out-of-range dependency; the operator decides whether to add them to the plan.
    """
    core = (inventory.get("core") or {}).get("manifest")
    apps = {
        entry["manifest"]["app_id"]: entry["manifest"]
        for entry in inventory.get("apps", [])
        if entry["state"] != "schema_retained"
    }
    for manifest in artifact_manifests:
        if manifest["kind"] == "core":
            core = manifest
        else:
            apps[manifest["app_id"]] = manifest
    removed = set(removals)
    blocked = [
        {"code": "installed_dependents", "component": dependent, "dependency": app_id}
        for app_id in sorted(removed & set(apps))
        for dependent in dependents_of(apps.values(), [app_id])
        if dependent not in removed
    ]
    if blocked:
        raise AgentError("installed_dependents", 409, details=blocked[:MAX_PROBLEM_DETAILS])
    if core is None:
        return None  # create_release reports the unidentified core base.
    remaining = [manifest for app_id, manifest in apps.items() if app_id not in removed]
    environment = backend.target_environment()
    django = target_django_version(config, [core, *remaining], environment)
    # First-party apps the runtime installs satisfy dependencies at the core's version (#254).
    provided = installed_bundled_app_ids(config)
    resolution = resolve_target(
        [core, *remaining], core=core["version"], environment=environment, django=django, provided=provided
    )
    if not resolution.ok:
        target = {"skeleton": core["version"], "python": environment["python_full_version"], "django": django}
        raise dependency_error(
            resolution, suggest and (lambda dependency, required: suggest(dependency, required, target))
        )
    return resolution


def ordered_entries(entries: JSON, order: Sequence[str]) -> list[JSON]:
    """Inventory apps dependency-first, so INSTALLED_APPS and ready() follow the graph."""
    rank = {app_id: index for index, app_id in enumerate(order)}
    return [
        entry
        for _, entry in sorted(entries.items(), key=lambda item: (item[0] not in rank, rank.get(item[0], 0), item[0]))
    ]


def create_release(
    config: "Config",
    store: "Store",
    backend: "Backend",
    inventory: JSON,
    artifacts: Sequence[str],
    removals: Sequence[str],
    release: Path,
) -> JSON:
    source = backend.active()
    release.mkdir(parents=True, exist_ok=False)
    old_entries = {entry["manifest"]["app_id"]: entry for entry in inventory.get("apps", [])}
    managed_packages = {
        entry["manifest"]["package_name"]
        for entry in old_entries.values()
        if entry["state"] != "project-local" and entry.get("origin") != "project-local"
    }
    copy_project(config, source, release, managed_packages=managed_packages)
    entries = {key: dict(entry) for key, entry in old_entries.items()}
    core, changed = _apply_artifacts(config, store, artifacts, entries, dict(inventory.get("core", {})))
    _apply_removals(entries, removals, changed)
    if not core.get("digest") or not core.get("manifest"):
        raise AgentError("unidentified_core_base", 409)
    manifests = [entry["manifest"] for entry in entries.values() if entry["state"] != "schema_retained"]
    environment = backend.target_environment()
    _install_core_wheels(config, store, release, inventory, core, changed)
    retained, archives = _materialize_apps(config, store, backend, release, source, entries, old_entries, removals)
    return _finalize(config, backend, release, inventory, core, entries, manifests, environment, retained, archives)


def _apply_artifacts(
    config: "Config", store: "Store", artifacts: Sequence[str], entries: JSON, core: JSON
) -> tuple[JSON, set[str]]:
    """The core and the apps of the plan's packages; ``(core, changed component ids)``."""
    changed = set()
    for artifact in artifacts:
        package = verified(store, config, artifact)
        manifest = package.manifest
        if manifest["kind"] == "core":
            core = _core_from_package(manifest, core, artifact)
            changed.add("django-skeleton")
            continue
        app_id = manifest["app_id"]
        entries[app_id] = _app_from_package(manifest, entries.get(app_id), artifact)
        changed.add(app_id)
    return core, changed


def _core_from_package(manifest: JSON, core: JSON, artifact: str) -> JSON:
    if core.get("version") and not _supports_source(manifest, core["version"]):
        raise AgentError("unsupported_core_upgrade", 409)
    if core.get("version") and Version(manifest["version"]) < Version(core["version"]):
        raise AgentError("downgrade_forbidden", 409)
    return {
        "manifest": manifest,
        "version": manifest["version"],
        "origin": "installed",
        "digest": artifact,
        "publisher": manifest["publisher"],
    }


def _app_from_package(manifest: JSON, previous: JSON | None, artifact: str) -> JSON:
    if previous:
        old = previous["manifest"]
        if any(old[key] != manifest[key] for key in ("app_label", "package_name", "app_config")):
            raise AgentError("component_identity_changed", 409)
        if previous.get("origin") == "project-local" or previous.get("state") == "project-local":
            raise AgentError("project_local_collision", 409)
        if Version(manifest["version"]) < Version(old["version"]):
            raise AgentError("downgrade_forbidden", 409)
        if not _supports_source(manifest, old["version"]):
            raise AgentError("unsupported_app_upgrade", 409)
        compare_history(old, manifest)
        # runtime.default_enabled is obsolete (#22): changing it activates nothing, so it is not refused.
    entry = {"manifest": manifest, "digest": artifact, "state": "installed", "origin": "installed"}
    if previous and (previous.get("activation_policy") == "explicit" or previous["state"] == "schema_retained"):
        entry["activation_policy"] = "explicit"
    return entry


def _apply_removals(entries: JSON, removals: Sequence[str], changed: set[str]) -> None:
    for app_id in removals:
        if app_id in changed or app_id not in entries or entries[app_id]["state"] != "installed":
            raise AgentError("invalid_uninstall_target", 409)
        if entries[app_id].get("origin") == "project-local":
            raise AgentError("project_local_removal", 409)
        entries[app_id]["state"] = "schema_retained"
        entries[app_id]["activation_policy"] = "explicit"


def _install_core_wheels(
    config: "Config", store: "Store", release: Path, inventory: JSON, core: JSON, changed: set[str]
) -> None:
    """Extract the core package's wheels and record the core files; migrations never change."""
    core_package = verified(store, config, core["digest"])
    extract_package(core_package, None, wheels_destination=release / "core-wheels")
    for path in (release / "core-wheels").iterdir():
        shutil.move(str(path), release / "wheels" / path.name)
    (release / "core-wheels").rmdir()
    core_wheels = [
        release / dependency["wheel"]
        for dependency in core["manifest"]["python_dependencies"]
        if canonicalize_name(dependency["name"]) == "django-skeleton"
    ]
    if len(core_wheels) != 1:
        raise AgentError("missing_core_wheel")
    core["files"] = wheel_files(core_wheels[0])
    core["origin"] = "installed"
    if "django-skeleton" in changed and inventory.get("core", {}).get("files"):
        old_core = inventory["core"]["files"]
        if any(
            core["files"].get(path, {}).get("sha256") != row["sha256"]
            for path, row in old_core.items()
            if "/migrations/" in path and path.endswith(".py")
        ):
            raise AgentError("core_migration_history_modified", 409)


def _materialize_apps(
    config: "Config",
    store: "Store",
    backend: "Backend",
    release: Path,
    source: Path,
    entries: JSON,
    old_entries: JSON,
    removals: Sequence[str],
) -> tuple[list[JSON], list[JSON]]:
    """Place each app in the release; ``(retained schema references, migration archives)``."""
    retained = []
    archives = []
    for app_id, entry in sorted(entries.items()):
        manifest = entry["manifest"]
        target = release / "applications" / manifest["package_name"]
        if entry.get("origin") == "project-local" or entry["state"] == "project-local":
            entry["path"] = str(_preserved_local_app(release, source, entry))
            continue
        if target.exists():
            # Only remove a previously managed path with a verified ownership journal.
            previous = old_entries.get(app_id)
            if not previous or previous.get("origin") == "project-local":
                raise AgentError("unowned_app_path_collision", 409)
            shutil.rmtree(target)
        package = verified(store, config, entry["digest"])
        if entry["state"] == "schema_retained":
            reference, archive = _retain_app(backend, release, source, app_id, entry, package, old_entries, removals)
            retained.append(reference)
            archives.append(archive)
        else:
            _extract_app(release, target, package, manifest["package_name"])
            entry.pop("retained_schema", None)
            entry["path"] = str(target)
    return retained, archives


def _preserved_local_app(release: Path, source: Path, entry: JSON) -> Path:
    previous_path = Path(entry["path"])
    if source not in previous_path.resolve().parents:
        raise AgentError("local_app_outside_project", 409)
    local_path = release / previous_path.relative_to(source)
    if not local_path.is_dir():
        raise AgentError("local_app_not_preserved", 409)
    return local_path


def _retain_app(
    backend: "Backend",
    release: Path,
    source: Path,
    app_id: str,
    entry: JSON,
    package: "VerifiedPackage",
    old_entries: JSON,
    removals: Sequence[str],
) -> tuple[JSON, JSON]:
    """Archive the historical migrations and the schema snapshot of a retired app."""
    manifest = entry["manifest"]
    archive_name = f"archive_{manifest['app_label']}"
    archive = release / "archives" / archive_name
    archive.mkdir()
    atomic_bytes(archive / "__init__.py", b"", 0o644)
    with ZipFile(package.path) as payload:
        for record in manifest["files"]:
            relative = record["path"]
            if relative.startswith("app/migrations/"):
                destination = archive / relative.removeprefix("app/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_bytes(destination, payload.read(relative), 0o644)
    snapshot = release / "retained" / f"{manifest['app_label']}.json"
    if app_id in removals:
        backend.export_schema(source, manifest["app_label"], snapshot)
    else:
        previous_schema = old_entries[app_id]["retained_schema"]
        if file_hash(previous_schema["path"]) != previous_schema["sha256"]:
            raise AgentError("retained_schema_hash_conflict", 409)
        shutil.copyfile(previous_schema["path"], snapshot)
    reference = {"path": str(snapshot), "sha256": file_hash(snapshot)}
    entry["retained_schema"] = reference
    entry["path"] = str(archive)
    return reference, {
        "app_label": manifest["app_label"],
        "package_name": manifest["package_name"],
        "path": str(archive),
        "migrations_module": f"{archive_name}.migrations",
        "digest": entry["digest"],
    }


def _extract_app(release: Path, target: Path, package: "VerifiedPackage", package_name: str) -> None:
    extraction = release / f"wheels-{package_name}"
    extract_package(package, target, wheels_destination=extraction)
    if extraction.exists():
        for path in extraction.iterdir():
            wheel = release / "wheels" / path.name
            if wheel.exists() and file_hash(wheel) != file_hash(path):
                raise AgentError("wheel_filename_collision", 409)
            if not wheel.exists():
                shutil.copyfile(path, wheel)
        shutil.rmtree(extraction)


def _finalize(
    config: "Config",
    backend: "Backend",
    release: Path,
    inventory: JSON,
    core: JSON,
    entries: JSON,
    manifests: Sequence[JSON],
    environment: Mapping[str, Any],
    retained: list[JSON],
    archives: list[JSON],
) -> JSON:
    """Lock, resolve, write the inventory and run the build checks of the prepared release."""
    lock = build_lock(config, release, [core["manifest"], *manifests], environment)
    resolution = resolve_target(
        [core["manifest"], *manifests],
        core=core["version"],
        environment=environment,
        django=lock["django"]["version"],
        provided=installed_bundled_app_ids(config),  # Same verdict as the plan check (#254).
    )
    if not resolution.ok:
        raise dependency_error(resolution)
    result = {
        "generation": inventory["generation"] + 1,
        "core": core,
        "apps": ordered_entries(entries, resolution.order),
        "retained_schema_paths": retained,
        "migration_archives": archives,
        "environment": environment,
        "runtime": {"python": environment["python_full_version"], "django": lock["django"]["version"]},
    }
    atomic_json(release / "inventory.json", result, 0o644)
    backend.prepare(release)
    backend.manage(release, ["check"], "build", code="static_checks_failed")
    backend.manage(release, ["collectstatic", "--noinput"], "build", code="collectstatic_failed")
    return result


def _supports_source(manifest: JSON, source: str) -> bool:
    return Version(source) in SpecifierSet(manifest["migrations"]["from_versions"])


def presentation_diff(before: JSON, after: JSON) -> list[JSON]:
    """Describe code-owned UI/configuration changes without altering signed manifests."""

    def active(inventory: JSON) -> JSON:
        return {
            entry["manifest"]["app_id"]: entry["manifest"]
            for entry in inventory.get("apps", [])
            if entry["state"] in {"installed", "project-local"}
        }

    previous, current = active(before), active(after)
    changes = []
    for app_id in sorted(previous.keys() | current.keys()):
        old, new = previous.get(app_id), current.get(app_id)
        manifest = cast(JSON, new or old)  # One of them exists: the key came from their union.
        base = {"app_id": app_id, "app_label": manifest["app_label"]}
        if old is None or new is None:
            changes.append(
                {**base, "resource": "group", "id": app_id, "action": "add" if new else "remove", "incompatible": False}
            )
        resources = []
        for item in (old, new):
            runtime = item.get("runtime", {}) if item else {}
            administration = runtime.get("administration", {})
            resources.append(
                {
                    "configuration": {field["name"]: field for field in runtime.get("configuration_fields", [])},
                    "configuration_section": {
                        section["id"]: section for section in administration.get("configuration_sections", [])
                    },
                    "section": {section["id"]: section for section in administration.get("sections", [])},
                    "screen": {screen["id"]: screen for screen in administration.get("screens", [])},
                    "model_layout": {spec["table_key"]: spec for spec in runtime.get("model_ui", [])},
                }
            )
        for kind in resources[0]:
            left, right = resources[0][kind], resources[1][kind]
            for key in sorted(left.keys() | right.keys()):
                first, second = left.get(key), right.get(key)
                if first == second:
                    continue
                incompatible = bool(
                    kind == "configuration"
                    and old
                    and new
                    and first
                    and (
                        second is None
                        or first["type"] != second["type"]
                        or first.get("max_length", 200) > second.get("max_length", 200)
                    )
                )
                changes.append(
                    {
                        **base,
                        "resource": kind,
                        "id": key,
                        "action": "add" if first is None else "remove" if second is None else "change",
                        "before": first,
                        "after": second,
                        "incompatible": incompatible,
                    }
                )
    return changes


def remove_release(release: StrPath) -> None:
    """Delete a release directory, frozen or not, without following links (#245)."""
    release = Path(release)
    if release.is_symlink():
        raise AgentError("release_path_is_symlink", 409)
    for path in release.rglob("*"):
        if not path.is_symlink():
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
    os.chmod(release, 0o755)
    shutil.rmtree(release)
