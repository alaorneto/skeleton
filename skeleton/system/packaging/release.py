"""Release packages (#492): one signed ``.skapp`` per core release.

A release package wraps the signed core package without changing it and adds, per target
(interpreter and platform), the hash-locked requirements and the wheels they pin, plus the
deployment agent wheel. It is verified with the same operator trust store as component packages,
and the core inside must carry the same publisher and version. Signature proves provenance, not
the absence of malicious code: the Python payload remains privileged.
"""

import hashlib
import io
import os
import stat
import tempfile
import zipfile
from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path

from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import Version

from . import limits, zipio
from .archive import _signature, inspect_package, validate_package
from .errors import PackageError, _fail
from .primitives import _json, _object, _path, _stable_id, _string, _version, canonical_json
from .signing import _private_key, _trusted_key, _verify
from .zipio import _read_member, _zip_members

CORE_COMPONENT = "django-skeleton"
RELEASE_FIELDS = (
    "release_format",
    "kind",
    "component_id",
    "name",
    "version",
    "publisher",
    "core",
    "targets",
    "files",
    "agent",
    "release_notes",
    "build",
)
RELEASE_REQUIRED = (
    "release_format",
    "kind",
    "component_id",
    "name",
    "version",
    "publisher",
    "core",
    "targets",
    "files",
)
ROLE_DIRECTORIES = {"packages": "package", "wheels": "wheel", "locks": "lock", "agent": "agent"}
# Fixed member metadata keeps two builds of the same inputs byte for byte identical.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
ZIP_MODE = (stat.S_IFREG | 0o644) << 16


@dataclass(frozen=True)
class VerifiedRelease:
    manifest: dict
    digest: str  # SHA-256 of the whole release package.
    path: Path
    core_manifest: dict  # Manifest of the embedded core package, verified with the same key.
    core_digest: str  # SHA-256 of the embedded core package.
    _public_key: object = field(default=None, repr=False, compare=False)
    _snapshot: bytes = field(default=None, repr=False, compare=False)


def _wheel_name(path):
    try:
        name, version, _, _ = parse_wheel_filename(path.rsplit("/", 1)[-1])
    except InvalidWheelFilename as exc:
        raise PackageError("Invalid wheel filename.") from exc
    return canonicalize_name(name), version


def package_kind(path):
    """Unverified ``kind`` of a package, only to choose its validator; never a trust decision."""
    snapshot = zipio._protected_snapshot(path, limits.MAX_ARCHIVE_BYTES)
    try:
        with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
            files = _zip_members(archive)
            if "manifest.json" not in files:
                _fail("Signed manifest is required.")
            manifest = _json(_read_member(archive, files["manifest.json"], limits.MAX_METADATA_BYTES))
    except (zipfile.BadZipFile, RuntimeError, OSError, KeyError) as exc:
        raise PackageError("Invalid package archive.") from exc
    kind = manifest.get("kind") if isinstance(manifest, dict) else None
    if kind not in ("app", "core", limits.BUNDLED_KIND, limits.RELEASE_KIND):
        _fail("Unknown package kind.")
    return kind


def _files(value):
    if not isinstance(value, list) or not value or len(value) > limits.MAX_MEMBERS:
        _fail("Invalid release file inventory.")
    inventory, seen = {}, set()
    for item in value:
        _object(item, ("path", "size", "sha256", "role"), ("path", "size", "sha256", "role"))
        path = _path(item["path"])
        if path.casefold() in seen:
            _fail("Duplicate release file.")
        seen.add(path.casefold())
        parts = path.split("/")
        if len(parts) != 2 or ROLE_DIRECTORIES.get(parts[0]) != item["role"]:
            _fail("Release file role does not match its directory.")
        if type(item["size"]) is not int or not 0 <= item["size"] <= limits.MAX_FILE_BYTES:
            _fail("Invalid release file size.")
        if not isinstance(item["sha256"], str) or not limits.SHA256.fullmatch(item["sha256"]):
            _fail("Invalid release file digest.")
        inventory[path] = item
    return inventory


def _core(value, inventory):
    _object(value, ("path", "sha256"), ("path", "sha256"))
    path = value["path"]
    if (
        not isinstance(path, str)
        or not path.startswith("packages/")
        or not path.endswith(".skapp")
        or inventory.get(path, {}).get("sha256") != value["sha256"]
    ):
        _fail("Release core package is not in the signed inventory.")
    if [item for item in inventory.values() if item["role"] == "package"] != [inventory[path]]:
        _fail("A release carries exactly one core package.")


def _environment(value):
    keys = limits.TARGET_ENVIRONMENT_KEYS
    _object(value, keys, keys)
    if any(not isinstance(value[key], str) or not value[key] or len(value[key]) > 128 for key in keys):
        _fail("Invalid release target environment.")
    return tuple(value[key] for key in keys)


def _targets(value, inventory):
    if not isinstance(value, list) or not 1 <= len(value) <= limits.MAX_RELEASE_TARGETS:  # Read at call time.
        _fail("Invalid release targets.")
    identities, environments, used_wheels, used_locks = set(), set(), set(), []
    for target in value:
        _object(target, ("id", "environment", "lock", "wheels"), ("id", "environment", "lock", "wheels"))
        if not isinstance(target["id"], str) or not limits.TARGET_ID.fullmatch(target["id"]):
            _fail("Invalid release target id.")
        if target["id"] in identities:
            _fail("Duplicate release target id.")
        identities.add(target["id"])
        environment = _environment(target["environment"])
        if environment in environments:
            _fail("Duplicate release target environment.")
        environments.add(environment)
        lock = "locks/" + target["id"] + ".txt"
        if target["lock"] != lock or inventory.get(lock, {}).get("role") != "lock":
            _fail("Release target lock is not in the signed inventory.")
        used_locks.append(lock)
        wheels = target["wheels"]
        if not isinstance(wheels, list) or len(set(wheels)) != len(wheels):
            _fail("Invalid release target wheels.")
        distributions = set()
        for wheel in wheels:
            if not isinstance(wheel, str) or inventory.get(wheel, {}).get("role") != "wheel":
                _fail("Release target wheel is not in the signed inventory.")
            name, _ = _wheel_name(wheel)
            if name in (CORE_COMPONENT, limits.AGENT_DISTRIBUTION):
                _fail("The core and the agent are not target dependency wheels.")
            if name in distributions:
                _fail("A target pins one wheel per distribution.")
            distributions.add(name)
            used_wheels.add(wheel)
    roles = {path: item["role"] for path, item in inventory.items()}
    if {path for path, role in roles.items() if role == "wheel"} != used_wheels:
        _fail("Every release wheel belongs to a target.")
    if sorted(path for path, role in roles.items() if role == "lock") != sorted(used_locks):
        _fail("Every release lock belongs to exactly one target.")


def _agent(value, inventory, version):
    if value is None:
        if any(item["role"] == "agent" for item in inventory.values()):
            _fail("Agent wheel is not declared.")
        return
    _object(value, ("version", "wheel", "sha256"), ("version", "wheel", "sha256"))
    wheel = value["wheel"]
    if (
        not isinstance(wheel, str)
        or not wheel.startswith("agent/")
        or inventory.get(wheel, {}).get("role") != "agent"
        or inventory[wheel]["sha256"] != value["sha256"]
    ):
        _fail("Release agent wheel is not in the signed inventory.")
    if [item for item in inventory.values() if item["role"] == "agent"] != [inventory[wheel]]:
        _fail("A release carries at most one agent wheel.")
    name, wheel_version = _wheel_name(wheel)
    agent_version = Version(_version(value["version"]))
    if name != limits.AGENT_DISTRIBUTION or not agent_version == wheel_version == Version(version):
        _fail("Agent wheel must be the agent distribution of the release version.")


def validate_release_manifest(value):
    _object(value, RELEASE_FIELDS, RELEASE_REQUIRED)
    if type(value["release_format"]) is not int or value["release_format"] not in limits.RELEASE_FORMATS:
        _fail("Unsupported release format.")
    if value["kind"] != limits.RELEASE_KIND or value["component_id"] != CORE_COMPONENT:
        _fail("Release packages carry the core component.")
    _string(value["name"], 200)
    _stable_id(value["publisher"])
    if _version(value["version"]) != value["version"]:
        _fail("Release version must be normalized.")
    inventory = _files(value["files"])
    _core(value["core"], inventory)
    _targets(value["targets"], inventory)
    _agent(value.get("agent"), inventory, value["version"])
    if "release_notes" in value and value["release_notes"] != "":
        _string(value["release_notes"], 10000)
    if "build" in value and value["build"] != "":
        _string(value["build"], 4096)
    return value


def _check_locks(manifest, contents):
    hashes = {item["path"]: item["sha256"] for item in manifest["files"]}
    for target in manifest["targets"]:
        try:
            text = contents[target["lock"]].decode("utf-8")
        except UnicodeError as exc:
            raise PackageError("Release lock must be UTF-8 text.") from exc
        if any(f"--hash=sha256:{hashes[wheel]}" not in text for wheel in target["wheels"]):
            _fail("Release target wheel is not pinned by its lock.")


def _embedded_core(manifest, data, trust_store):
    """Verify the embedded core package from a private temporary copy of its bytes."""
    with tempfile.TemporaryDirectory(prefix="skeleton-release-") as directory:
        path = Path(directory) / "core.skapp"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        if trust_store is None:
            inspected = inspect_package(path)
            core_manifest, core_digest = inspected["manifest"], inspected["digest"]
        else:
            package = validate_package(path, trust_store)
            core_manifest, core_digest = package.manifest, package.digest
    if (
        core_manifest["kind"] != "core"
        or core_manifest["publisher"] != manifest["publisher"]
        or Version(core_manifest["version"]) != Version(manifest["version"])
    ):
        _fail("Release and core package must share kind, publisher and version.")
    return core_manifest, core_digest


def _validate_release_archive(path, trust_store):
    path = Path(path)
    snapshot = zipio._protected_snapshot(path, limits.MAX_ARCHIVE_BYTES)
    digest = hashlib.sha256(snapshot).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
            files = _zip_members(archive)
            if not {"manifest.json", "manifest.sig"} <= files.keys():
                _fail("Signed manifest is required.")
            raw = _read_member(archive, files["manifest.json"], limits.MAX_METADATA_BYTES)
            manifest = validate_release_manifest(_json(raw))
            if raw != canonical_json(manifest):
                _fail("Manifest must use canonical JSON and normalized versions.")
            signature = _signature(archive, files)
            key = None
            if trust_store is not None:
                key = _trusted_key(manifest["publisher"], trust_store)
                _verify(key, signature, raw)
            inventory = {item["path"]: item for item in manifest["files"]}
            if files.keys() != inventory.keys() | {"manifest.json", "manifest.sig"}:
                _fail("Archive files differ from the complete signed inventory.")
            contents = {}
            for name, item in inventory.items():
                maximum = limits.MAX_METADATA_BYTES if item["role"] == "lock" else None
                data = _read_member(archive, files[name], maximum)
                if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                    _fail("Payload hash or size mismatch.")
                if item["role"] in ("lock", "package"):
                    contents[name] = data
            _check_locks(manifest, contents)
    except (zipfile.BadZipFile, RuntimeError, OSError, KeyError) as exc:
        raise PackageError("Invalid package archive.") from exc
    core_trust = None if trust_store is None else {manifest["publisher"]: key}
    core_manifest, core_digest = _embedded_core(manifest, contents[manifest["core"]["path"]], core_trust)
    return VerifiedRelease(manifest, digest, path.absolute(), core_manifest, core_digest, key, snapshot)


def validate_release(path, trust_store):
    if trust_store is None:
        _fail("Trusted publisher configuration is required.")
    return _validate_release_archive(path, trust_store)


def inspect_release(path, trust_store=None):
    release = _validate_release_archive(path, trust_store)
    return {
        "trusted": trust_store is not None,
        "digest": release.digest,
        "manifest": release.manifest,
        "core": {"digest": release.core_digest, "manifest": release.core_manifest},
    }


def read_release_member(verified, name):
    """Bytes of one inventory file, from the verified snapshot (never the path again)."""
    if not isinstance(verified, VerifiedRelease) or verified._snapshot is None:
        _fail("Only verified release packages may be read.")
    inventory = {item["path"]: item for item in verified.manifest["files"]}
    if name not in inventory:
        _fail("Release member is not in the signed inventory.")
    try:
        with zipfile.ZipFile(io.BytesIO(verified._snapshot)) as archive:
            data = _read_member(archive, archive.getinfo(name))
    except (zipfile.BadZipFile, RuntimeError, OSError, KeyError) as exc:
        raise PackageError("Invalid package archive.") from exc
    if hashlib.sha256(data).hexdigest() != inventory[name]["sha256"]:
        _fail("Release member changed after verification.")
    return data


def match_release_target(manifest, environment):
    """The target whose four environment keys equal ``environment``'s, or None."""
    keys = limits.TARGET_ENVIRONMENT_KEYS
    for target in manifest["targets"]:
        if all(environment.get(key) == target["environment"][key] for key in keys):
            return target
    return None


def _payload(source):
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        _fail("Payload must be regular files, never links.")
    if source.stat().st_size > limits.MAX_FILE_BYTES:
        _fail("Payload file too large.")
    return source.read_bytes()


def _add_member(contents, name, data):
    _path(name)
    if name in contents and contents[name] != data:
        _fail("Distinct release files share a name.")
    contents[name] = data


def build_release(
    core_package,
    output_path,
    private_key_path,
    *,
    targets,
    agent_wheel=None,
    name="Skeleton",
    release_notes="",
    build="",
):
    """Wrap a signed core package with per-target locks and wheels; the same key signs both."""
    core_package = Path(core_package)
    core = inspect_package(core_package)["manifest"]
    if core["kind"] != "core":
        _fail("Release packages wrap a core package.")
    key = _private_key(private_key_path)
    public = key.public_key()
    try:
        validate_package(core_package, {core["publisher"]: public})
    except PackageError as exc:
        raise PackageError("Release and core must be signed by the same publisher key.") from exc
    output = Path(output_path)
    if output.exists():
        _fail("Output already exists; releases are immutable.")
    contents = {}
    core_path = "packages/" + core_package.name
    _add_member(contents, core_path, _payload(core_package))
    declared = []
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("id"), str):
            _fail("Invalid release targets.")
        wheels = []
        for wheel in target.get("wheels", ()):
            member = "wheels/" + Path(wheel).name
            _add_member(contents, member, _payload(wheel))
            wheels.append(member)
        lock = "locks/" + target["id"] + ".txt"
        _add_member(contents, lock, _payload(target["lock"]))
        declared.append(
            {"id": target["id"], "environment": target.get("environment"), "lock": lock, "wheels": sorted(wheels)}
        )
    agent = None
    if agent_wheel is not None:
        member = "agent/" + Path(agent_wheel).name
        _add_member(contents, member, _payload(agent_wheel))
        _, agent_version = _wheel_name(member)
        agent = {"version": str(agent_version), "wheel": member, "sha256": hashlib.sha256(contents[member]).hexdigest()}
    files = [
        {
            "path": path,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "role": ROLE_DIRECTORIES[path.split("/", 1)[0]],
        }
        for path, data in sorted(contents.items())
    ]
    manifest = {
        "release_format": limits.RELEASE_FORMATS[-1],
        "kind": limits.RELEASE_KIND,
        "component_id": CORE_COMPONENT,
        "name": name,
        "version": core["version"],
        "publisher": core["publisher"],
        "core": {"path": core_path, "sha256": hashlib.sha256(contents[core_path]).hexdigest()},
        "targets": declared,
        "files": files,
    }
    if agent is not None:
        manifest["agent"] = agent
    if release_notes:
        manifest["release_notes"] = release_notes
    if build:
        manifest["build"] = build
    manifest = validate_release_manifest(manifest)
    raw = canonical_json(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open("xb") as handle:
            created = True
            with zipfile.ZipFile(handle, "w") as archive:
                members = [
                    ("manifest.json", raw),
                    ("manifest.sig", b64encode(key.sign(raw))),
                    *sorted(contents.items()),
                ]
                for member, data in members:
                    info = zipfile.ZipInfo(member, date_time=ZIP_DATE)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = ZIP_MODE
                    info.create_system = 3  # Unix, whatever the build host.
                    archive.writestr(info, data)
        validate_release(output, {manifest["publisher"]: public})
    except Exception:
        if created:  # Only what this process created: a concurrent output is never removed.
            output.unlink(missing_ok=True)
        raise
    return output
