"""Signed, expiring release catalogues and their entries."""

import io
import re
import zipfile
from base64 import b64decode, b64encode
from datetime import UTC, datetime
from pathlib import Path

from . import archive as artifacts
from . import limits
from .archive import VerifiedPackage
from .errors import PackageError, _fail
from .limits import CATALOG_VERSIONS, SHA256
from .primitives import (
    _compatibility,
    _identifier,
    _json,
    _object,
    _path,
    _specifier,
    _stable_id,
    _string,
    _timestamp,
    _version,
    canonical_json,
)
from .signing import _private_key, _trusted_key, _verify
from .zipio import _read_member


def _catalog(value):
    _object(
        value,
        ("catalog_version", "publisher", "generation", "expires_at", "entries"),
        ("catalog_version", "publisher", "generation", "expires_at", "entries"),
    )
    if (
        type(value["catalog_version"]) is not int
        or value["catalog_version"] not in CATALOG_VERSIONS
        or type(value["generation"]) is not int
        or value["generation"] < 1
    ):
        _fail("Invalid catalog format/generation.")
    _stable_id(value["publisher"])
    _timestamp(value["expires_at"])
    if not isinstance(value["entries"], list) or len(value["entries"]) > limits.MAX_MEMBERS:
        _fail("Invalid release catalog entries.")
    identities = set()
    for entry in value["entries"]:
        fields = (
            "component_id",
            "kind",
            "version",
            "channel",
            "compatibility",
            "artifact",
            "digest",
            "signature",
            "publisher",
            "notes",
            "migrations",
        )
        if value["catalog_version"] >= 2:
            # v2 entries carry the dependency contract so previews can suggest releases.
            fields = (*fields, "dependencies", "conflicts")
        _object(entry, fields, fields)
        for relation in ("dependencies", "conflicts"):
            ranges = entry.get(relation, {})
            if not isinstance(ranges, dict):
                _fail("Invalid catalog dependency ranges.")
            for dependency, spec in ranges.items():
                _stable_id(dependency)
                _specifier(spec)
        _stable_id(entry["component_id"])
        _stable_id(entry["publisher"])
        _stable_id(entry["channel"])
        if entry["kind"] not in ("core", "app") or (
            entry["kind"] == "core" and entry["component_id"] != "django-skeleton"
        ):
            _fail("Invalid catalog component kind.")
        if _version(entry["version"]) != entry["version"]:
            _fail("Catalog version must be normalized.")
        _compatibility(entry["compatibility"])
        _string(entry["artifact"], 2048)
        from urllib.parse import urlsplit

        url = urlsplit(entry["artifact"])
        if url.scheme:
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
                _fail("Catalog artifacts require HTTPS or relative safe paths.")
        else:
            _path(entry["artifact"])
        if not isinstance(entry["digest"], str) or not SHA256.fullmatch(entry["digest"]):
            _fail("Invalid catalog artifact digest.")
        try:
            if len(b64decode(entry["signature"], validate=True)) != 64:
                _fail("Invalid catalog artifact signature.")
        except (ValueError, TypeError) as exc:
            raise PackageError("Invalid catalog artifact signature.") from exc
        if not isinstance(entry["notes"], str) or len(entry["notes"]) > 10000:
            _fail("Invalid release notes.")
        migrations = _object(entry["migrations"], ("leaves", "from_versions", "labels"), ("leaves", "from_versions"))
        if not isinstance(migrations["leaves"], list) or any(
            not isinstance(leaf, str) or not re.fullmatch(r"[A-Za-z0-9_]+", leaf) for leaf in migrations["leaves"]
        ):
            _fail("Invalid catalog migration leaves.")
        if len(set(migrations["leaves"])) != len(migrations["leaves"]):
            _fail("Duplicate catalog migration leaves.")
        _specifier(migrations["from_versions"])
        if "labels" in migrations:
            if not isinstance(migrations["labels"], list):
                _fail("Invalid catalog migration labels.")
            for label in migrations["labels"]:
                _identifier(label)
        identity = (entry["component_id"], entry["version"], entry["channel"])
        if identity in identities:
            _fail("Duplicate catalog release identity.")
        identities.add(identity)
    return value


def build_catalog(entries, output_path, private_key_path, *, publisher, generation, expires_at):
    catalog = _catalog(
        {
            "catalog_version": CATALOG_VERSIONS[-1],
            "publisher": publisher,
            "generation": generation,
            "expires_at": expires_at,
            "entries": entries,
        }
    )
    if _timestamp(expires_at) <= datetime.now(UTC):
        _fail("Cannot publish an already expired catalog.")
    signature = b64encode(_private_key(private_key_path).sign(canonical_json(catalog))).decode("ascii")
    output = Path(output_path)
    with output.open("xb") as handle:
        handle.write(canonical_json({"catalog": catalog, "signature": signature}))
    return output


def validate_catalog(path_or_dict, trust_store, *, minimum_generation=0, channels=("stable",), now=None):
    if isinstance(path_or_dict, dict):
        envelope = path_or_dict
    else:
        path = Path(path_or_dict)
        if path.stat().st_size > limits.MAX_METADATA_BYTES:
            _fail("Catalog size limit exceeded.")
        envelope = _json(path.read_bytes())
    _object(envelope, ("catalog", "signature"), ("catalog", "signature"))
    catalog = _catalog(envelope["catalog"])
    _verify(_trusted_key(catalog["publisher"], trust_store), envelope["signature"], canonical_json(catalog))
    if _timestamp(catalog["expires_at"]) <= (now or datetime.now(UTC)):
        _fail("Release catalog is expired.")
    if catalog["generation"] < minimum_generation:
        _fail("Release catalog generation rollback.")
    return {**catalog, "entries": [e for e in catalog["entries"] if e["channel"] in channels]}


def catalog_entry(package, *, artifact, channel="stable", notes=""):
    if not isinstance(package, VerifiedPackage) or package._public_key is None:
        _fail("Catalog requires trusted verified packages.")
    fresh = artifacts.validate_package(package.path, {package.manifest["publisher"]: package._public_key})
    if fresh.digest != package.digest or fresh.manifest != package.manifest:
        _fail("Catalog artifact changed after verification.")
    with zipfile.ZipFile(io.BytesIO(fresh._snapshot)) as archive:
        signature = _read_member(archive, archive.getinfo("manifest.sig"), limits.MAX_METADATA_BYTES).decode("ascii")
    m = fresh.manifest
    return {
        "component_id": m.get("app_id", m.get("component_id")),
        "kind": m["kind"],
        "version": m["version"],
        "channel": channel,
        "compatibility": m["compatibility"],
        "artifact": artifact,
        "digest": package.digest,
        "signature": signature,
        "publisher": m["publisher"],
        "notes": notes or m.get("release_notes", ""),
        "migrations": m["migrations"],
        "dependencies": m["dependencies"],
        "conflicts": m.get("conflicts", {}),
    }
