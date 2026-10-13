"""Offline core publication and explicit source-copy adoption utilities."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import zipfile

from packaging.utils import canonicalize_name
from packaging.version import Version

from skeleton.system.packages import PackageError, build_artifact, validate_manifest, validate_package


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
                inventory[name] = {"sha256": _digest(archive.read(info)), "size": info.file_size}
            elif ".dist-info/" not in name or "/" not in name:
                raise PackageError("Core wheel contains project-owned files.")
            if name.endswith(".dist-info/METADATA"):
                from email.parser import BytesParser
                metadata.append(BytesParser().parsebytes(archive.read(info)))
        if len(metadata) != 1 or canonicalize_name(metadata[0]["Name"]) != "django-skeleton":
            raise PackageError("Expected a django-skeleton core wheel.")
        if "skeleton/__init__.py" not in inventory or "skeleton/conf/base.py" not in inventory:
            raise PackageError("Core wheel lacks reusable defaults.")
        return {"version": metadata[0]["Version"], "files": inventory}


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
    with zipfile.ZipFile(verified.path) as archive:
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
        target.write_bytes(archive.read(core_candidates[0]))
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
    return {"base_version": base["version"], "base_digest": verified.digest,
            "classification": "equivalent" if not (added or removed or changed) else "fork",
            "added": added, "removed": removed, "changed": changed, "files": expected}


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
        if installed.is_relative_to(project / "skeleton") or not installed.is_file() or _digest(installed.read_bytes()) != info["sha256"]:
            raise PackageError("Installed distribution must match all base files outside the source copy.")
    backup = Path(backup_root).resolve()
    if backup.is_relative_to(project) or project.is_relative_to(backup):
        raise PackageError("Backup must be a separate directory outside the project.")
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = backup / ("skeleton-" + verified.digest)
    if target.exists():
        raise PackageError("Base backup already exists; adoption is not repeated.")
    # Rename preserves every historical migration and fails closed across filesystems.
    os.rename(project / "skeleton", target)
    record = {key: value for key, value in plan.items() if key != "files"}
    record["backup"] = str(target)
    (backup / ("adoption-" + verified.digest + ".json")).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
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
    for name in ("adoption-plan", "adopt"):
        command = commands.add_parser(name)
        command.add_argument("--project-root", required=True)
        command.add_argument("--base-release", required=True)
        command.add_argument("--trust-store", required=True, help="Private JSON publisher key-id to PEM path mapping")
        if name == "adopt":
            command.add_argument("--backup-root", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            print(build_core_release(args.wheel, args.manifest, args.output, args.private_key, wheels=args.dependency_wheel))
        else:
            trust = json.loads(Path(args.trust_store).read_text(encoding="utf-8"))
            verified = validate_package(args.base_release, trust)
            result = (adopt_source_core(args.project_root, verified, args.backup_root) if args.command == "adopt"
                      else source_adoption_plan(args.project_root, verified))
            print(json.dumps({key: value for key, value in result.items() if key != "files"}, sort_keys=True))
    except (PackageError, OSError, ValueError) as exc:
        parser.exit(1, f"Core distribution operation failed: {exc}\n")


if __name__ == "__main__":
    main()
