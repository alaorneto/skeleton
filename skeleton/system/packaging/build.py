"""Building and re-signing artifacts from an app source, with static migration leaves."""

import ast
import hashlib
import io
import json
import os
import zipfile
from base64 import b64encode
from pathlib import Path

from packaging.version import Version

from skeleton.system.presentation import (
    required_presentation_core,
    validate_runtime_presentation,
)

from . import archive as artifacts
from . import limits
from .errors import PackageError, _fail
from .limits import BUILD_EXCLUDED_DIRECTORIES, SERVICE_API_CORE_MINIMUM
from .manifest import load_manifest, validate_manifest
from .primitives import _path, _specifier, _stable_id, canonical_json
from .signing import _private_key
from .zipio import _read_member


def _role(path):
    if path.startswith("wheels/"):
        return "wheel"
    if "/migrations/" in path:
        return "migration"
    if "/templates/" in path:
        return "template"
    if "/static/" in path:
        return "static"
    if "/docs/" in path:
        return "documentation"
    return "code" if path.endswith(".py") else "data"


def build_artifact(manifest, payloads, output_path, private_key_path):
    """Build app/core from a complete mapping of archive path -> regular local file."""
    m = json.loads(canonical_json(manifest))
    if type(m.get("manifest_version")) is not int or m["manifest_version"] not in (1, 2):
        _fail("Unsupported manifest format.")
    # Building a new artifact is a new publication, unlike reading/resigning a
    # historical artifact whose canonical bytes must remain unchanged.
    m["manifest_version"] = 2
    if m.get("kind") == "app":
        validate_runtime_presentation(m["runtime"], m["app_label"], error_class=PackageError)
        minimum = required_presentation_core(m["runtime"])
        if "service_api" in m["runtime"]:
            minimum = max(minimum, SERVICE_API_CORE_MINIMUM, key=Version)
        m["compatibility"]["skeleton"] = _specifier(m["compatibility"]["skeleton"] + ",>=" + minimum)
    m["files"] = []
    contents = {}
    for name, source in sorted(payloads.items()):
        _path(name)
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            _fail("Payload must be regular files, never links.")
        if source.stat().st_size > limits.MAX_FILE_BYTES:
            _fail("Payload file too large.")
        data = source.read_bytes()
        contents[name] = data
        m["files"].append(
            {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "role": _role(name)}
        )
    for dependency in m["python_dependencies"]:
        if dependency["wheel"] not in contents:
            _fail("Declared wheel is missing.")
        dependency["sha256"] = hashlib.sha256(contents[dependency["wheel"]]).hexdigest()
    m = validate_manifest(m)
    raw = canonical_json(m)
    key = _private_key(private_key_path)
    output = Path(output_path)
    if output.exists():
        _fail("Output already exists; releases are immutable.")
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            created = True
            archive.writestr("manifest.json", raw)
            archive.writestr("manifest.sig", b64encode(key.sign(raw)))
            for name, data in contents.items():
                archive.writestr(name, data)
        artifacts.validate_package(output, {m["publisher"]: key.public_key()})
    except Exception:
        if created:  # Only what this process created: a concurrent output is never removed (#255).
            output.unlink(missing_ok=True)
        raise
    return output


def _literal_dependencies(tree):
    """Same-file Migration.dependencies as (label, name) pairs; None when not a plain literal."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            for statement in node.body:
                if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "dependencies" for target in statement.targets
                ):
                    if not isinstance(statement.value, (ast.List, ast.Tuple)):
                        return None
                    pairs = []
                    for element in statement.value.elts:
                        if (
                            isinstance(element, ast.Tuple)
                            and len(element.elts) == 2
                            and all(
                                isinstance(item, ast.Constant) and isinstance(item.value, str) for item in element.elts
                            )
                        ):
                            pairs.append((element.elts[0].value, element.elts[1].value))
                        # Calls such as swappable_dependency() never name this app's own migrations.
                    return pairs
            return []
    return None


def static_migration_leaves(migrations_dir, app_label):
    """Leaf migration names from source, without importing code; None when undeterminable."""
    directory = Path(migrations_dir)
    if not directory.is_dir():
        return set()
    names, referenced = set(), set()
    for path in sorted(directory.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            return None
        dependencies = _literal_dependencies(tree)
        if dependencies is None:
            return None
        names.add(path.stem)
        referenced.update(name for label, name in dependencies if label == app_label)
    return names - referenced


def check_migration_leaves(app_root, manifest):
    """Message describing a leaves mismatch, or None when they agree or cannot be determined."""
    actual = static_migration_leaves(Path(app_root) / "migrations", manifest["app_label"])
    declared = set(manifest["migrations"]["leaves"])
    if actual is None or actual == declared:
        return None
    return f"Manifest migrations.leaves {sorted(declared)} differ from the migration files {sorted(actual)}."


def _scan_application_root(root, manifest_source):
    """Archive payloads plus (code, message, path) violations the build rejects, in walk order."""
    payloads, violations = {}, []
    for current, directories, filenames in os.walk(root, followlinks=False):
        for name in list(directories):
            candidate = Path(current) / name
            if candidate.is_symlink():
                relative = candidate.relative_to(root).as_posix()
                violations.append(("symlink", "Application root contains a symlink.", relative))
            if name in BUILD_EXCLUDED_DIRECTORIES:
                directories.remove(name)
        for name in filenames:
            source = Path(current) / name
            relative = source.relative_to(root).as_posix()
            if source.is_symlink():
                violations.append(("symlink", "Application root contains a symlink.", relative))
                continue
            if name.endswith((".pyc", ".pyo")) or source.resolve() == manifest_source:
                continue
            if (
                any(part.startswith(".") for part in source.relative_to(root).parts)
                or name == ".env"
                or name.endswith(".sqlite3")  # the instance configuration store and its copies
                or name.endswith((".pem", ".key"))
            ):
                violations.append(("private_file", "Private files must not be packaged.", relative))
                continue
            payloads["app/" + relative] = source
    return payloads, violations


def build_package(app_root, manifest_path, output_path, private_key_path, wheels=()):
    root = Path(app_root).resolve()
    manifest = load_manifest(manifest_path)
    if manifest["kind"] != "app":
        _fail("build_package requires an application manifest.")
    leaves_problem = check_migration_leaves(root, manifest)
    if leaves_problem:
        _fail(leaves_problem)
    if "service_api" not in manifest["runtime"]:
        # Historical artifacts without it still load; a new publication must declare its service API.
        _fail("New releases require runtime.service_api (docs/service-api.md).")
    output = Path(output_path).resolve()
    if output.is_relative_to(root):
        _fail("Build output must be outside the application root.")
    payloads, violations = _scan_application_root(root, Path(manifest_path).resolve())
    if violations:
        _fail(violations[0][1])
    for wheel in wheels:
        wheel = Path(wheel)
        name = "wheels/" + wheel.name
        if name in payloads:
            _fail("Duplicate supplied wheel.")
        payloads[name] = wheel
    # Runtime uses the same canonical contract after relocation; the package's
    # root signed manifest remains authoritative (no circular inventory hash).
    return build_artifact(manifest, payloads, output_path, private_key_path)


def sign_package(path, output_path, private_key_path, *, publisher=None):
    """Inspect all hashes/structure first, then republish with an operator signature."""
    package = artifacts._validate_archive(path)
    m = package.manifest
    if publisher is not None:
        m["publisher"] = _stable_id(publisher)
    m = validate_manifest(m)
    output = Path(output_path)
    if output.exists():
        _fail("Signed output must be a new artifact.")
    key = _private_key(private_key_path)
    raw = canonical_json(m)
    created = False
    try:
        with (
            zipfile.ZipFile(io.BytesIO(package._snapshot)) as source,
            zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as target,
        ):
            created = True
            target.writestr("manifest.json", raw)
            target.writestr("manifest.sig", b64encode(key.sign(raw)))
            for item in m["files"]:
                data = _read_member(source, source.getinfo(item["path"]))
                if hashlib.sha256(data).hexdigest() != item["sha256"]:
                    _fail("Payload changed during signing.")
                target.writestr(item["path"], data)
        artifacts.validate_package(output, {m["publisher"]: key.public_key()})
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise
    return output
