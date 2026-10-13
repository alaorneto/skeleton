"""Verified artifact composition, hash-locked environments and exact ownership diffs."""

from email.parser import BytesParser
import hashlib
import os
from pathlib import Path
import re
import shutil
from zipfile import ZipFile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from skeleton.system.packages import PackageError, extract_package, validate_dependency_graph, validate_package, validate_wheel_set

from .storage import AgentError, atomic_bytes, atomic_json, digest, file_hash, inventory_files, load_json


def verified(store, config, artifact):
    try:
        return validate_package(store.path("uploads", artifact), config["approved_publishers"])
    except PackageError:
        raise AgentError("package_verification_failed", 409) from None


def wheel_files(path):
    with ZipFile(path) as archive:
        return {name: {"sha256": hashlib.sha256(archive.read(name)).hexdigest(), "size": len(archive.read(name))} for name in archive.namelist() if name.startswith("skeleton/") and not name.endswith("/")}


def migrations(manifest):
    return {entry["path"]: entry["sha256"] for entry in manifest["files"] if entry["path"].startswith("app/migrations/") and entry["path"].endswith(".py")}


def compare_history(old, new):
    previous = migrations(old)
    current = migrations(new)
    if any(current.get(path) != value for path, value in previous.items()):
        raise AgentError("migration_history_modified", 409)


def parse_lock(path, target_environment=None):
    text = Path(path).read_text().replace("\\\n", " ")
    rows = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split(" #", 1)[0].strip()
        hashes = re.findall(r"--hash=sha256:([a-f0-9]{64})", line)
        requirement_text = re.sub(r"\s*--hash=sha256:[a-f0-9]{64}", "", line).strip()
        try:
            requirement = Requirement(requirement_text)
        except Exception:
            raise AgentError("invalid_requirements_lock") from None
        pins = list(requirement.specifier)
        if len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version or requirement.url or not hashes:
            raise AgentError("unlocked_dependency")
        name = canonicalize_name(requirement.name)
        if requirement.marker and not requirement.marker.evaluate(target_environment or default_environment()):
            continue
        if name in rows:
            raise AgentError("duplicate_locked_dependency")
        rows[name] = {"version": pins[0].version, "hashes": set(hashes), "extras": set(requirement.extras)}
    return rows


def wheel_metadata(path):
    with ZipFile(path) as archive:
        entries = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(entries) != 1:
            raise AgentError("invalid_wheel")
        metadata = BytesParser().parsebytes(archive.read(entries[0]))
        return canonicalize_name(metadata["Name"]), metadata["Version"]


def build_lock(config, release, manifests, target_environment=None):
    target_environment = target_environment or default_environment()
    rows = parse_lock(config["requirements_lock"], target_environment)
    wheels = release / "wheels"
    selections = {}
    for manifest in manifests:
        for dependency in manifest["python_dependencies"]:
            marker = dependency.get("marker")
            if marker:
                from packaging.markers import Marker
                if not Marker(marker).evaluate(target_environment):
                    continue
            name = canonicalize_name(dependency["name"])
            version = dependency["version"]
            if name in selections and Version(selections[name]["version"]) != Version(version):
                raise AgentError("dependency_version_conflict", 409)
            record = selections.setdefault(name, {"version": version, "hashes": set(), "extras": rows.get(name, {}).get("extras", set())})
            record["hashes"].add(dependency["sha256"])
    rows.update(selections)
    if "django-skeleton" not in rows or "django" not in rows:
        raise AgentError("missing_core_dependencies", 409)
    wheelhouse = Path(config["wheelhouse"])
    available = {}
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
        lines.append(f"{name}{extras}=={row['version']} " + " ".join(f"--hash=sha256:{value}" for value in sorted(available[name])))
    atomic_bytes(release / "requirements.lock", ("\n".join(lines) + "\n").encode(), 0o644)
    return rows


def copy_project(config, source, release, *, managed_packages):
    source = Path(source)
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", ".env", ".private", ".venv", "*.sqlite3", "node_modules")
    def ignore(directory, names):
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


def owned_snapshot(release):
    result = inventory_files(release)
    result.pop("owned-files.json", None)
    for path, row in result.items():
        if path.startswith("applications/"):
            row["owner"] = "app:" + path.split("/")[1]
        elif path.startswith(".venv/"):
            row["owner"] = "environment"
        elif path.startswith(("staticfiles/", "archives/", "retained/", "wheels/")) or path in ("inventory.json", "requirements.lock"):
            row["owner"] = "deployment"
        else:
            row["owner"] = "project"
    return result


def check_drift(release, expected=None):
    expected = expected or load_json(Path(release) / "owned-files.json")
    if owned_snapshot(release) != expected:
        raise AgentError("local_file_drift", 409)


def file_diff(before, after):
    result = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        result.append({"path": path, "owner": (new or old).get("owner", "deployment"), "action": "add" if old is None else "remove" if new is None else "change", "before": old.get("sha256") if old else None, "after": new.get("sha256") if new else None})
    return result


def freeze(release):
    for path in Path(release).rglob("*"):
        if not path.is_symlink():
            mode = path.stat().st_mode
            os.chmod(path, 0o555 if path.is_dir() or mode & 0o111 else 0o444)
    os.chmod(release, 0o555)


def create_release(config, store, backend, inventory, artifacts, removals, release):
    source = backend.active()
    release.mkdir(parents=True, exist_ok=False)
    old_entries = {entry["manifest"]["app_id"]: entry for entry in inventory.get("apps", [])}
    managed_packages = {entry["manifest"]["package_name"] for entry in old_entries.values()
                        if entry["state"] != "project-local" and entry.get("origin") != "project-local"}
    copy_project(config, source, release, managed_packages=managed_packages)
    entries = {key: dict(entry) for key, entry in old_entries.items()}
    core = dict(inventory.get("core", {}))
    changed = set()
    for artifact in artifacts:
        package = verified(store, config, artifact)
        manifest = package.manifest
        if manifest["kind"] == "core":
            if core.get("version") and not _supports_source(manifest, core["version"]):
                raise AgentError("unsupported_core_upgrade", 409)
            if core.get("version") and Version(manifest["version"]) < Version(core["version"]):
                raise AgentError("downgrade_forbidden", 409)
            core = {"manifest": manifest, "version": manifest["version"], "origin": "installed", "digest": artifact, "publisher": manifest["publisher"]}
            changed.add("django-skeleton")
            continue
        app_id = manifest["app_id"]
        previous = entries.get(app_id)
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
            if any(old["runtime"][key] != manifest["runtime"][key] for key in ("default_enabled", "configuration_fields")):
                raise AgentError("configuration_upgrade_requires_review", 409)
        entry = {"manifest": manifest, "digest": artifact, "state": "installed", "origin": "installed"}
        if previous and (previous.get("activation_policy") == "explicit" or previous["state"] == "schema_retained"):
            entry["activation_policy"] = "explicit"
        entries[app_id] = entry
        changed.add(app_id)
    for app_id in removals:
        if app_id in changed or app_id not in entries or entries[app_id]["state"] != "installed":
            raise AgentError("invalid_uninstall_target", 409)
        if entries[app_id].get("origin") == "project-local":
            raise AgentError("project_local_removal", 409)
        entries[app_id]["state"] = "schema_retained"
        entries[app_id]["activation_policy"] = "explicit"
    if not core.get("digest") or not core.get("manifest"):
        raise AgentError("unidentified_core_base", 409)
    manifests = [entry["manifest"] for entry in entries.values() if entry["state"] != "schema_retained"]
    environment = backend.target_environment()
    core_package = verified(store, config, core["digest"])
    extract_package(core_package, None, wheels_destination=release / "core-wheels")
    for path in (release / "core-wheels").iterdir():
        shutil.move(str(path), release / "wheels" / path.name)
    (release / "core-wheels").rmdir()
    core_wheels = [release / dependency["wheel"] for dependency in core["manifest"]["python_dependencies"] if canonicalize_name(dependency["name"]) == "django-skeleton"]
    if len(core_wheels) != 1:
        raise AgentError("missing_core_wheel")
    core["files"] = wheel_files(core_wheels[0])
    core["origin"] = "installed"
    if "django-skeleton" in changed and inventory.get("core", {}).get("files"):
        old_core = inventory["core"]["files"]
        if any(core["files"].get(path, {}).get("sha256") != row["sha256"] for path, row in old_core.items() if "/migrations/" in path and path.endswith(".py")):
            raise AgentError("core_migration_history_modified", 409)
    retained = []
    archives = []
    for app_id, entry in sorted(entries.items()):
        manifest = entry["manifest"]
        package_name = manifest["package_name"]
        target = release / "applications" / package_name
        if entry.get("origin") == "project-local" or entry["state"] == "project-local":
            previous_path = Path(entry["path"])
            if source not in previous_path.resolve().parents:
                raise AgentError("local_app_outside_project", 409)
            local_path = release / previous_path.relative_to(source)
            if not local_path.is_dir():
                raise AgentError("local_app_not_preserved", 409)
            entry["path"] = str(local_path)
            continue
        if target.exists():
            # Only remove a previously managed path with a verified ownership journal.
            previous = old_entries.get(app_id)
            if not previous or previous.get("origin") == "project-local":
                raise AgentError("unowned_app_path_collision", 409)
            shutil.rmtree(target)
        package = verified(store, config, entry["digest"])
        if entry["state"] == "schema_retained":
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
            retained.append(reference)
            archives.append({"app_label": manifest["app_label"], "package_name": package_name, "path": str(archive), "migrations_module": f"{archive_name}.migrations", "digest": entry["digest"]})
            entry["path"] = str(archive)
        else:
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
            entry.pop("retained_schema", None)
            entry["path"] = str(target)
    lock = build_lock(config, release, [core["manifest"], *manifests], environment)
    try:
        validate_dependency_graph([core["manifest"], *manifests], core=core["version"], python=environment["python_full_version"], django=lock["django"]["version"], target_environment=environment)
    except PackageError:
        raise AgentError("target_runtime_incompatible", 409) from None
    result = {"generation": inventory["generation"] + 1, "core": core, "apps": list(entries.values()), "retained_schema_paths": retained, "migration_archives": archives, "environment": environment}
    atomic_json(release / "inventory.json", result, 0o644)
    backend.prepare(release)
    backend.manage(release, ["check"], "build", code="static_checks_failed")
    backend.manage(release, ["collectstatic", "--noinput"], "build", code="collectstatic_failed")
    return result


def _supports_source(manifest, source):
    from packaging.specifiers import SpecifierSet
    return Version(source) in SpecifierSet(manifest["migrations"]["from_versions"])
