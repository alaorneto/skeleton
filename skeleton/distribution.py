"""Offline core publication and explicit source-copy adoption utilities."""

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import zipfile
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from skeleton.system.packages import (
    MAX_METADATA_BYTES,
    PackageError,
    _read_member,
    build_artifact,
    load_trust_store,
    validate_manifest,
    validate_package,
)
from skeleton.versioning import normalize_version, upgrade_sources


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def wheel_inventory(wheel):
    """Read the release base without executing its Python payload."""
    inventory = {}
    with zipfile.ZipFile(wheel) as archive:
        seen = set()
        metadata = []
        for info in archive.infolist():
            name = info.filename
            if name in seen or name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                raise PackageError("Invalid core wheel path.")
            seen.add(name)
            if info.is_dir():
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise PackageError("Core wheel cannot contain symlinks.")
            if name.startswith("skeleton/"):
                inventory[name] = {"sha256": _digest(_read_member(archive, info)), "size": info.file_size}
            elif ".dist-info/" not in name or "/" not in name:
                raise PackageError("Core wheel contains project-owned files.")
            if name.endswith(".dist-info/METADATA"):
                from email.parser import BytesParser

                metadata.append(BytesParser().parsebytes(_read_member(archive, info, MAX_METADATA_BYTES)))
        if len(metadata) != 1 or canonicalize_name(metadata[0]["Name"]) != "django-skeleton":
            raise PackageError("Expected a django-skeleton core wheel.")
        if "skeleton/__init__.py" not in inventory or "skeleton/conf/base.py" not in inventory:
            raise PackageError("Core wheel lacks reusable defaults.")
        return {
            "version": metadata[0]["Version"],
            "files": inventory,
            # Read from the same METADATA, for core_manifest (#494).
            "requires_python": metadata[0].get("Requires-Python"),
            "requires_dist": list(metadata[0].get_all("Requires-Dist", ())),
        }


def _django_range(requirements):
    """``>=X.Y,<X.(Y+1)`` from the core wheel's exact Django pin (``==X.Y.Z`` or ``==X.Y``)."""
    pins = [Requirement(value) for value in requirements if canonicalize_name(Requirement(value).name) == "django"]
    specifiers = list(pins[0].specifier) if len(pins) == 1 and not pins[0].marker else []
    if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
        raise PackageError("Core wheel must pin Django to derive compatibility.")
    release = Version(specifiers[0].version).release
    if len(release) not in (2, 3):
        raise PackageError("Core wheel must pin Django to derive compatibility.")
    return f">={release[0]}.{release[1]},<{release[0]}.{release[1] + 1}"


def core_manifest(
    wheel_path,
    *,
    publisher,
    known_versions=(),
    earliest_source=None,
    name="Skeleton",
    description="Skeleton core release",
    release_notes="",
    build="",
):
    """The core manifest of a wheel (#494): versions from the wheel, sources from the line policy.

    The manifest uses the input format of build_core_release (``files: []``); dependencies travel
    per target in the release package, not here.
    """
    wheel = Path(wheel_path)
    base = wheel_inventory(wheel)
    version = normalize_version(base["version"])
    sources = upgrade_sources(version, known_versions, earliest=earliest_source)
    try:
        django = _django_range(base["requires_dist"])
    except InvalidRequirement as exc:
        raise PackageError("Core wheel must pin Django to derive compatibility.") from exc
    if not base["requires_python"]:
        raise PackageError("Core wheel must declare Requires-Python.")
    data = wheel.read_bytes()
    manifest = {
        "manifest_version": 2,
        "kind": "core",
        "component_id": "django-skeleton",
        "name": name,
        "description": description,
        "version": version,
        "publisher": publisher,
        "compatibility": {"skeleton": sources, "python": base["requires_python"], "django": django},
        "dependencies": {},
        "migrations": {"leaves": [], "from_versions": sources},
        "files": [],
        "python_dependencies": [
            {
                "name": "django-skeleton",
                "version": base["version"],
                "wheel": "wheels/" + wheel.name,
                "sha256": _digest(data),
            }
        ],
        "lifecycle": {"data_retention": "retain", "maintenance": True, "backup": True},
    }
    if release_notes:
        manifest["release_notes"] = release_notes
    if build:
        manifest["build"] = build
    # validate_manifest checks the inventoried form; the input form keeps files empty.
    inventoried = {
        **manifest,
        "files": [{"path": "wheels/" + wheel.name, "size": len(data), "sha256": _digest(data), "role": "wheel"}],
    }
    checked = validate_manifest(inventoried)
    return {**checked, "files": []}


def build_core_release(wheel_path, manifest_path, output_path, private_key_path, *, wheels=()):
    wheel = Path(wheel_path)
    base = wheel_inventory(wheel)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("kind") != "core" or Version(manifest.get("version", "0")) != Version(base["version"]):
        raise PackageError("Core manifest and wheel versions differ.")
    # Payload inventory is generated and signed by the same app-package implementation.
    payloads = {f"wheels/{wheel.name}": wheel}
    for dependency in wheels:
        dependency = Path(dependency)
        if dependency.suffix != ".whl" or f"wheels/{dependency.name}" in payloads:
            raise PackageError("Dependencies must be distinct wheels.")
        payloads[f"wheels/{dependency.name}"] = dependency
    return build_artifact(manifest, payloads, output_path, private_key_path)


def _release_wheel(verified, destination):
    # The verified snapshot, never the path again: the file may have changed since (#255).
    with zipfile.ZipFile(io.BytesIO(verified._snapshot)) as archive:
        core_candidates = []
        for item in verified.manifest["files"]:
            path = item["path"]
            if path.startswith("wheels/") and path.endswith(".whl"):
                from packaging.utils import parse_wheel_filename

                distribution, _, _, _ = parse_wheel_filename(Path(path).name)
                if canonicalize_name(distribution) == "django-skeleton":
                    core_candidates.append(path)
        if len(core_candidates) != 1:
            raise PackageError("Core release must contain exactly one core wheel.")
        target = destination / Path(core_candidates[0]).name
        target.write_bytes(_read_member(archive, archive.getinfo(core_candidates[0])))
        return target


def source_adoption_plan(project_root, verified):
    """Compare against a cryptographically verified, identified base, never HEAD."""
    if verified.manifest["kind"] != "core":
        raise PackageError("Adoption requires a signed core release base.")
    import tempfile

    with tempfile.TemporaryDirectory(prefix="skeleton-base-") as tmp:
        base = wheel_inventory(_release_wheel(verified, Path(tmp)))
    project = Path(project_root).resolve()
    source = project / "skeleton"
    if not source.is_dir() or source.is_symlink():
        raise PackageError("Project must contain an ordinary skeleton source directory.")
    actual = {}
    for path in source.rglob("*"):
        relative = path.relative_to(project).as_posix()
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise PackageError("Source core contains a symlink.")
        if path.is_file():
            actual[relative] = _digest(path.read_bytes())
    expected = base["files"]
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    changed = sorted(path for path in set(expected) & set(actual) if actual[path] != expected[path]["sha256"])
    return {
        "base_version": base["version"],
        "base_digest": verified.digest,
        "classification": "equivalent" if not (added or removed or changed) else "fork",
        "added": added,
        "removed": removed,
        "changed": changed,
        "files": expected,
    }


def adopt_source_core(project_root, verified, backup_root):
    """Cut over only an equivalent copy to an already installed, equivalent wheel.

    Applications, config, manage.py, data and retained archives are never modified.
    The old core (including historical migrations) is kept outside the project.
    """
    project = Path(project_root).resolve()
    plan = source_adoption_plan(project, verified)
    if plan["classification"] != "equivalent":
        raise PackageError("Locally modified core requires a reviewed fork migration; no files changed.")
    try:
        distribution = importlib.metadata.distribution("django-skeleton")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PackageError("Install the verified base wheel outside the project before adoption.") from exc
    if Version(distribution.version) != Version(plan["base_version"]):
        raise PackageError("Installed core does not match the identified release base.")
    for relative, info in plan["files"].items():
        installed = Path(distribution.locate_file(relative)).resolve()
        if (
            installed.is_relative_to(project / "skeleton")
            or not installed.is_file()
            or _digest(installed.read_bytes()) != info["sha256"]
        ):
            raise PackageError("Installed distribution must match all base files outside the source copy.")
    backup = Path(backup_root).resolve()
    if backup.is_relative_to(project) or project.is_relative_to(backup):
        raise PackageError("Backup must be a separate directory outside the project.")
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = backup / ("skeleton-" + verified.digest)
    if target.exists():
        raise PackageError("Base backup already exists; adoption is not repeated.")
    record = {key: value for key, value in plan.items() if key != "files"}
    record["backup"] = str(target)
    # Rename preserves every historical migration and fails closed across filesystems.
    os.rename(project / "skeleton", target)
    try:
        (backup / ("adoption-" + verified.digest + ".json")).write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        os.rename(target, project / "skeleton")  # Without the record, undo the cut-over (#255).
        raise
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(prog="skeleton-core")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Package an already built core wheel as a signed release")
    build.add_argument("--wheel", required=True)
    build.add_argument("--manifest", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--private-key", required=True)
    build.add_argument("--dependency-wheel", action="append", default=[])
    generated = commands.add_parser("manifest", help="Generate the core manifest of a wheel (line policy, #494)")
    generated.add_argument("--wheel", required=True)
    generated.add_argument("--publisher", required=True)
    generated.add_argument("--output", required=True)
    generated.add_argument("--known-version", action="append", default=[], help="Already published version")
    generated.add_argument("--earliest-source", help="Floor of the first automated release")
    generated.add_argument("--release-notes-file")
    generated.add_argument("--build", default="")
    for name in ("adoption-plan", "adopt"):
        command = commands.add_parser(name)
        command.add_argument("--project-root", required=True)
        command.add_argument("--base-release", required=True)
        command.add_argument("--trust-store", required=True, help="Private JSON publisher key-id to PEM path mapping")
        if name == "adopt":
            command.add_argument("--backup-root", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            notes = ""
            if args.release_notes_file:
                path = Path(args.release_notes_file)
                if path.stat().st_size > 64 * 1024:
                    raise PackageError("Release notes file is too large.")
                notes = path.read_text(encoding="utf-8")
            value = core_manifest(
                args.wheel,
                publisher=args.publisher,
                known_versions=args.known_version,
                earliest_source=args.earliest_source,
                release_notes=notes,
                build=args.build,
            )
            with Path(args.output).open("xb") as handle:
                handle.write(json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
            print(args.output)
        elif args.command == "build":
            print(
                build_core_release(
                    args.wheel, args.manifest, args.output, args.private_key, wheels=args.dependency_wheel
                )
            )
        else:
            trust = load_trust_store(args.trust_store)
            verified = validate_package(args.base_release, trust)
            result = (
                adopt_source_core(args.project_root, verified, args.backup_root)
                if args.command == "adopt"
                else source_adoption_plan(args.project_root, verified)
            )
            print(json.dumps({key: value for key, value in result.items() if key != "files"}, sort_keys=True))
    except (PackageError, OSError, ValueError) as exc:
        parser.exit(1, f"Core distribution operation failed: {exc}\n")


if __name__ == "__main__":
    main()
