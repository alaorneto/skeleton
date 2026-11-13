"""Canonical JSON and the small validators every manifest and catalogue check is built from."""

import json
import re
import unicodedata
from datetime import datetime

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from skeleton.versioning import normalize_version, validate_specifier

from .errors import PackageError, _fail
from .limits import IDENTIFIER, STABLE_ID


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        _fail("Unknown or missing manifest fields.")
    return value


def _string(value, maximum=4096):
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        _fail("Invalid manifest string.")
    return value


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _fail("Invalid Python/Django identifier.")
    return value


def _stable_id(value):
    if not isinstance(value, str) or not STABLE_ID.fullmatch(value):
        _fail("Invalid stable component identity.")
    return value


def _path(value):
    _string(value, 512)
    if "\\" in value or value.startswith("/") or unicodedata.normalize("NFC", value) != value:
        _fail("Unsafe archive path.")
    parts = value.split("/")
    if any(not p or p in (".", "..") or ":" in p or p.rstrip(" .") != p or any(ord(c) < 32 for c in p) for p in parts):
        _fail("Unsafe archive path.")
    if any(
        p.split(".")[0].casefold()
        in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
        for p in parts
    ):
        _fail("Reserved archive path.")
    return value


# A dotted Python module path; prefixed by the package of the app that must own it.
MODULE_PATH = r"[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*"


def _owned_module(value, prefix):
    """value is a dotted module inside prefix (``applications.<package>.``)."""
    return isinstance(value, str) and re.fullmatch(re.escape(prefix) + MODULE_PATH, value) is not None


def _version(value):
    try:
        return normalize_version(value)
    except (TypeError, ValueError) as exc:
        raise PackageError(str(exc)) from exc


def _python_version(value):
    try:
        return Version(_string(value))
    except InvalidVersion as exc:
        raise PackageError("Invalid Python distribution/runtime version.") from exc


def _specifier(value):
    try:
        return validate_specifier(value)
    except (TypeError, ValueError) as exc:
        raise PackageError(str(exc)) from exc


def _compatibility(value):
    _object(value, ("skeleton", "python", "django"), ("skeleton", "python", "django"))
    return {key: _specifier(spec) for key, spec in value.items()}


def _core_floor_at_least(specifier, minimum):
    """Whether a compatibility.skeleton range excludes every core older than minimum."""
    floor = Version(minimum)
    return any(
        item.operator in (">=", ">", "==", "~=") and Version(item.version.removesuffix(".*")) >= floor
        for item in SpecifierSet(specifier)
    )


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("Duplicate JSON object key.")
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: _fail("Invalid JSON number."))
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise PackageError("Invalid release JSON.") from exc


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PackageError("Invalid catalog timestamp.") from exc
    if parsed.tzinfo is None:
        _fail("Catalog timestamps require an explicit timezone.")
    return parsed
