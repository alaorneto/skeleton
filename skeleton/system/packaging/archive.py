"""Signed artifacts: verification of the immutable snapshot, inspection and extraction."""

import ast
import hashlib
import io
import shutil
import zipfile
from base64 import b64decode
from dataclasses import dataclass, field
from pathlib import Path

from packaging.utils import canonicalize_name

from . import limits, zipio
from .errors import PackageError, _fail
from .manifest import required_documents, validate_manifest
from .primitives import _json, canonical_json
from .signing import _trusted_key, _verify
from .wheels import _claim_wheel, _wheel
from .zipio import _read_member, _zip_members


@dataclass(frozen=True)
class VerifiedPackage:
    manifest: dict
    digest: str
    path: Path
    _public_key: object = field(default=None, repr=False, compare=False)
    _snapshot: bytes = field(default=None, repr=False, compare=False)


def _module_path(module, package_name):
    """Relative candidate files of a module declared under applications.<package_name>."""
    relative = module[len("applications." + package_name) + 1 :].replace(".", "/")
    return f"{relative}.py", f"{relative}/__init__.py"


def _documentation(manifest, contents):
    root = "app/" + manifest["runtime"]["documentation_root"]
    for filename, headings in required_documents(manifest).items():
        try:
            text = contents[root + "/" + filename].decode("utf-8")
        except (KeyError, UnicodeError) as exc:
            raise PackageError("Required app documentation is missing.") from exc
        if any(heading not in text for heading in headings):
            _fail("App documentation lacks required contract/ADR sections.")


def _entrypoint(manifest, archive, files):
    """Verify declared identities without importing publisher code."""

    try:
        tree = ast.parse(_read_member(archive, files["app/apps.py"]))
    except (SyntaxError, UnicodeError) as exc:
        raise PackageError("Application entrypoint is not valid Python.") from exc
    name = manifest["app_config"].rsplit(".", 1)[1]
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    if len(classes) != 1:
        _fail("Declared AppConfig class is absent or ambiguous.")
    attributes = {}
    for node in classes[0].body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    attributes[target.id] = node.value
    label = attributes.get("label")
    package = attributes.get("name")
    if not isinstance(label, ast.Constant) or label.value != manifest["app_label"]:
        _fail("AppConfig must explicitly preserve its declared Django label.")
    expected = "applications." + manifest["package_name"]
    if not (
        (isinstance(package, ast.Name) and package.id == "__package__")
        or (isinstance(package, ast.Constant) and package.value == expected)
    ):
        _fail("AppConfig name must be its managed namespace or __package__.")
    modules = [manifest["runtime"].get(key) for key in ("urls", "api_urls")]
    modules.append(manifest["runtime"].get("service_api", {}).get("module"))
    for module in modules:
        if module:
            if not any("app/" + path in files for path in _module_path(module, manifest["package_name"])):
                _fail("Declared runtime module is absent.")
    for leaf in manifest["migrations"]["leaves"]:
        if f"app/migrations/{leaf}.py" not in files:
            _fail("Declared migration leaf is absent.")


def _signature(archive, files):
    signature = _read_member(archive, files["manifest.sig"], 256)
    try:
        if len(b64decode(signature, validate=True)) != 64:
            _fail("Invalid release signature encoding.")
    except (ValueError, TypeError) as exc:
        raise PackageError("Invalid release signature encoding.") from exc
    return signature


def _nested_wheel(manifest, name, item, data):
    """Validate one nested wheel: (expanded bytes, members, the claim the caller makes after the limits)."""
    dependency = next(d for d in manifest["python_dependencies"] if d["wheel"] == name)
    destinations, owned_roots, expanded, members, _ = _wheel(data, dependency, core=manifest["kind"] == "core")
    owner = {
        "name": canonicalize_name(dependency["name"]),
        "version": dependency["version"],
        "sha256": item["sha256"],
    }
    return expanded, members, (destinations, owned_roots, owner)


def _validate_archive(path, trust_store=None):
    path = Path(path)
    snapshot = zipio._protected_snapshot(path, limits.MAX_ARCHIVE_BYTES)
    digest = hashlib.sha256(snapshot).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
            files = _zip_members(archive)
            if not {"manifest.json", "manifest.sig"} <= files.keys():
                _fail("Signed manifest is required.")
            raw = _read_member(archive, files["manifest.json"], limits.MAX_METADATA_BYTES)
            manifest = validate_manifest(_json(raw))
            if raw != canonical_json(manifest):
                _fail("Manifest must use canonical JSON and normalized versions/ranges.")
            signature = _signature(archive, files)
            key = _trusted_key(manifest["publisher"], trust_store) if trust_store is not None else None
            if key is not None:
                _verify(key, signature, raw)
            inventory = {i["path"]: i for i in manifest["files"]}
            if files.keys() != inventory.keys() | {"manifest.json", "manifest.sig"}:
                _fail("Archive files differ from the complete signed inventory.")
            docs = {}
            wheel_ownership, wheel_namespaces = {}, {}
            expanded_total = sum(info.file_size for info in files.values())
            member_total = len(files)
            for name, item in inventory.items():
                data = _read_member(archive, files[name])
                if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                    _fail("Payload hash or size mismatch.")
                if name.startswith("wheels/"):
                    expanded, members, claim = _nested_wheel(manifest, name, item, data)
                    expanded_total += expanded
                    member_total += members
                    if member_total > limits.MAX_WHEELHOUSE_MEMBERS:
                        _fail("Combined nested wheel member limit exceeded.")
                    if expanded_total > limits.MAX_TOTAL_BYTES:
                        _fail("Combined nested wheel expansion limit exceeded.")
                    _claim_wheel(*claim, wheel_ownership, wheel_namespaces)
                if name.startswith("app/" + manifest.get("runtime", {}).get("documentation_root", "\x00") + "/"):
                    docs[name] = data
            if manifest["kind"] == "app":
                if not {"app/__init__.py", "app/apps.py"} <= inventory.keys():
                    _fail("Application package entrypoint is missing.")
                _documentation(manifest, docs)
                _entrypoint(manifest, archive, files)
            elif not manifest["files"]:
                _fail("Core artifact is empty.")
            return VerifiedPackage(manifest, digest, path.absolute(), key, snapshot)
    except (zipfile.BadZipFile, RuntimeError, OSError, KeyError) as exc:
        raise PackageError("Invalid package archive.") from exc


def validate_package(path, trust_store):
    if trust_store is None:
        _fail("Trusted publisher configuration is required.")
    return _validate_archive(path, trust_store)


def inspect_package(path, trust_store=None):
    package = _validate_archive(path, trust_store)
    return {"trusted": trust_store is not None, "digest": package.digest, "manifest": package.manifest}


def _extraction_roots(manifest, destination, wheels_destination):
    """Exact, separate, absent destinations without symlink ancestors."""
    roots = {}
    if manifest["kind"] == "app":
        if destination is None:
            _fail("Application destination is required.")
        roots["app"] = Path(destination).absolute()
    elif destination is not None:
        _fail("Core artifacts contain isolated wheels only.")
    if manifest["python_dependencies"]:
        if wheels_destination is None:
            _fail("An explicit isolated wheels destination is required.")
        roots["wheels"] = Path(wheels_destination).absolute()
    for root in roots.values():
        if root.exists() or root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            _fail("Extraction destination exists or has symlink ancestors.")
    if len(roots) == 2 and (
        roots["app"].is_relative_to(roots["wheels"]) or roots["wheels"].is_relative_to(roots["app"])
    ):
        _fail("App and wheels destinations must be separate.")
    return roots


def _write_payload(fresh, roots):
    """Every inventory file from the verified snapshot, plus the canonical manifest beside the app."""
    with zipfile.ZipFile(io.BytesIO(fresh._snapshot)) as archive:
        for item in fresh.manifest["files"]:
            prefix, relative = item["path"].split("/", 1)
            target = roots[prefix] / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            data = _read_member(archive, archive.getinfo(item["path"]))
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                _fail("Artifact changed during extraction.")
            with target.open("xb") as handle:
                handle.write(data)
            target.chmod(0o644)
        if "app" in roots:
            with (roots["app"] / "manifest.json").open("xb") as handle:
                handle.write(canonical_json(fresh.manifest))
            with (roots["app"] / "manifest.sig").open("xb") as handle:
                handle.write(_read_member(archive, archive.getinfo("manifest.sig"), limits.MAX_METADATA_BYTES))


def extract_package(verified, destination, *, wheels_destination=None):
    """Reverify and exclusively create exact app root and separate wheel root.

    No merge/overwrite, symlink ancestors, or wheels beneath the app directory.
    The canonical manifest is copied beside app files for AppManifest.from_json.
    """
    if not isinstance(verified, VerifiedPackage) or verified._public_key is None:
        _fail("Only trusted verified artifacts may be extracted.")
    fresh = validate_package(verified.path, {verified.manifest["publisher"]: verified._public_key})
    if fresh.digest != verified.digest or fresh.manifest != verified.manifest:
        _fail("Artifact changed after validation.")
    roots = _extraction_roots(fresh.manifest, destination, wheels_destination)
    created = []
    try:
        for root in roots.values():
            root.mkdir(parents=True, exist_ok=False)
            created.append(root)
        _write_payload(fresh, roots)
    except Exception:
        for root in created:
            shutil.rmtree(root, ignore_errors=True)  # The original error is the one that matters (#255).
        raise
    return roots
