"""Application and core manifests: validation, loading and the template of a new app."""

import json
import re
from pathlib import Path

from packaging.markers import InvalidMarker, Marker
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from skeleton.system.presentation import (
    FIELD_DIMENSIONS_CORE_MINIMUM,
    MANIFEST_V2_CORE_MINIMUM,
    required_presentation_core,
    validate_runtime_presentation,
)

from . import limits
from .errors import PackageError, _fail
from .limits import (
    BUNDLED_KIND,
    BUNDLED_LABEL_PREFIX,
    BUNDLED_PACKAGE,
    DOCUMENT_HEADINGS,
    RESERVED_NAMES,
    SERVICE_API_CORE_MINIMUM,
    SERVICE_API_DOCUMENT,
    SERVICE_API_HEADINGS,
    SERVICE_CLASS,
    SHA256,
)
from .primitives import (
    _compatibility,
    _core_floor_at_least,
    _identifier,
    _json,
    _object,
    _owned_module,
    _path,
    _python_version,
    _specifier,
    _stable_id,
    _string,
    _version,
    canonical_json,
)


def _validate_service_api(value, prefix):
    """runtime.service_api: one module inside the app package and simple, unique class names."""
    _object(value, ("module", "classes"), ("module", "classes"))
    module = value["module"]
    if not _owned_module(module, prefix):
        _fail("runtime.service_api.module must belong to the application.")
    classes = value["classes"]
    if (
        not isinstance(classes, list)
        or not 1 <= len(classes) <= limits.MAX_SERVICE_CLASSES
        or len(set(classes)) != len(classes)
        or any(not isinstance(name, str) or not SERVICE_CLASS.fullmatch(name) for name in classes)
    ):
        _fail("runtime.service_api.classes requires unique simple class names.")


MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "kind",
        "app_id",
        "component_id",
        "app_label",
        "package_name",
        "app_config",
        "name",
        "description",
        "version",
        "publisher",
        "compatibility",
        "dependencies",
        "conflicts",
        "runtime",
        "migrations",
        "files",
        "python_dependencies",
        "lifecycle",
        "release_notes",
        "build",
    }
)
MANIFEST_REQUIRED = frozenset(
    {
        "manifest_version",
        "kind",
        "name",
        "description",
        "version",
        "publisher",
        "compatibility",
        "dependencies",
        "migrations",
        "files",
        "python_dependencies",
        "lifecycle",
    }
)
APPLICATION_KINDS = ("app", BUNDLED_KIND)


def _validate_header(value):
    """Fields, format, kind and the scalar identity of any manifest; returns the canonical copy."""
    _object(value, MANIFEST_FIELDS, MANIFEST_REQUIRED)
    m = json.loads(canonical_json(value))
    if (
        type(m["manifest_version"]) is not int
        or m["manifest_version"] not in (1, 2)
        or m["kind"] not in ("app", "core", BUNDLED_KIND)
    ):
        _fail("Unsupported manifest format.")
    _string(m["name"], 200)
    _string(m["description"])
    _stable_id(m["publisher"])
    m["version"] = _version(m["version"])
    m["compatibility"] = _compatibility(m["compatibility"])
    return m


def _validate_application_identity(m):
    """Label, package and AppConfig namespace of an app; returns (label, prefix, bundled)."""
    bundled = m["kind"] == BUNDLED_KIND
    if set(("app_id", "app_label", "package_name", "app_config", "runtime")) - set(m) or "component_id" in m:
        _fail("Application identity/runtime is required.")
    _stable_id(m["app_id"])
    label, package = _identifier(m["app_label"]), _identifier(m["package_name"])
    if label.startswith("sk_") or label in RESERVED_NAMES or package in RESERVED_NAMES:
        _fail("Reserved core namespace.")
    # skeleton_* belongs to the first-party apps shipped inside the core wheel (#66).
    if label.startswith(BUNDLED_LABEL_PREFIX) != bundled:
        _fail("The skeleton_ label prefix is reserved to first-party apps of the core.")
    if bundled and (m["files"] or m["python_dependencies"]):
        _fail("First-party apps ship inside the core wheel, without files or wheels of their own.")
    prefix = f"{BUNDLED_PACKAGE}.{package}." if bundled else f"applications.{package}."
    if not isinstance(m["app_config"], str) or not re.fullmatch(
        re.escape(prefix) + r"apps\.[A-Za-z][A-Za-z0-9_]*", m["app_config"]
    ):
        _fail("AppConfig must belong to the managed application namespace.")
    return label, prefix, bundled


def _runtime_fields(m, bundled):
    runtime_fields = (
        "urls",
        "api_urls",
        "default_enabled",
        "configuration_fields",
        "navigation",
        "documentation_root",
    )
    if m["manifest_version"] == 2:
        runtime_fields += ("administration", "model_ui", "service_api")
        if not _core_floor_at_least(m["compatibility"]["skeleton"], MANIFEST_V2_CORE_MINIMUM):
            _fail("Manifest v2 requires a core minimum of " + MANIFEST_V2_CORE_MINIMUM + ".")
    if bundled:
        if m["manifest_version"] != 2:
            _fail("First-party apps use manifest v2.")
        runtime_fields += ("user_urls",)
    return runtime_fields


def _validate_configuration_fields(m, r):
    names = set()
    for config in r["configuration_fields"]:
        config_fields = ("name", "type", "default", "max_length")
        if m["manifest_version"] == 2:
            config_fields += ("label", "help_text", "section_id")
        _object(config, config_fields, ("name", "type", "default"))
        name = _identifier(config["name"])
        if m["manifest_version"] == 2 and name in {"enabled", "revision", "csrfmiddlewaretoken"}:
            _fail("Reserved application configuration control name.")
        if name in names or any(s in name for s in ("secret", "password", "token", "private_key")):
            _fail("Duplicate or secret application configuration.")
        names.add(name)
        expected = {"str": str, "int": int, "bool": bool}.get(config["type"])
        maximum = config.get("max_length", 200)
        if (
            expected is None
            or type(config["default"]) is not expected
            or type(maximum) is not int
            or not 1 <= maximum <= 10000
            or (expected is str and len(config["default"]) > maximum)
        ):
            _fail("Invalid scalar configuration field.")


def _validate_navigation(r, label):
    for entry in r["navigation"]:
        _object(entry, ("label", "url_name", "table_key", "action"), ("label", "url_name", "table_key"))
        _string(entry["label"], 200)
        _string(entry["url_name"], 200)
        if (
            not isinstance(entry["table_key"], str)
            or not re.fullmatch(re.escape(label) + r"\.[a-z][a-z0-9_]*", entry["table_key"])
            or entry.get("action", "read")
            not in ("read", "create", "update", "delete", "restore", "manage_access", "audit")
        ):
            _fail("Invalid navigation ownership/action.")


def _validate_presentation(m, r, label, prefix):
    """Manifest v2 presentation and service API, with the core floors they require."""
    validate_runtime_presentation(r, label, error_class=PackageError)
    if required_presentation_core(r) == FIELD_DIMENSIONS_CORE_MINIMUM and not _core_floor_at_least(
        m["compatibility"]["skeleton"], FIELD_DIMENSIONS_CORE_MINIMUM
    ):
        _fail("Field dimensions require a core minimum of 0.3.0.")
    if "service_api" in r:
        _validate_service_api(r["service_api"], prefix)
        if not _core_floor_at_least(m["compatibility"]["skeleton"], SERVICE_API_CORE_MINIMUM):
            _fail("runtime.service_api requires a core minimum of " + SERVICE_API_CORE_MINIMUM + ".")


def _validate_runtime(m, label, prefix, bundled):
    r = _object(
        m["runtime"],
        _runtime_fields(m, bundled),
        ("default_enabled", "configuration_fields", "navigation", "documentation_root"),
    )
    if type(r["default_enabled"]) is not bool:
        _fail("default_enabled must be boolean.")
    for key in ("urls", "api_urls", "user_urls"):
        if r.get(key) is not None and (not isinstance(r[key], str) or not _owned_module(r[key], prefix)):
            _fail("Runtime module must belong to the application.")
    _path(r["documentation_root"])
    if not isinstance(r["configuration_fields"], list) or not isinstance(r["navigation"], list):
        _fail("Runtime fields and navigation require lists.")
    _validate_configuration_fields(m, r)
    _validate_navigation(r, label)
    if m["manifest_version"] == 2:
        _validate_presentation(m, r, label, prefix)


def _validate_core_identity(m):
    if m.get("component_id") != "django-skeleton" or any(
        k in m for k in ("app_id", "app_label", "package_name", "app_config", "runtime")
    ):
        _fail("Invalid core component identity.")


def _validate_relations(m):
    identity = m.get("app_id", m.get("component_id"))
    if not isinstance(m["dependencies"], dict):
        _fail("Dependencies require an app-ID/version-range mapping.")
    for dependency, spec in m["dependencies"].items():
        _stable_id(dependency)
        if dependency == identity:
            _fail("Self dependency.")
        if m["kind"] in APPLICATION_KINDS and dependency == "django-skeleton":
            _fail("Applications declare the core range in compatibility.skeleton, not dependencies.")
        _specifier(spec)
    conflicts = m.get("conflicts", {})
    if not isinstance(conflicts, dict):
        _fail("Conflicts require an app-ID/version-range mapping.")
    for dependency, spec in conflicts.items():
        _stable_id(dependency)
        _specifier(spec)


def _validate_migrations(m):
    """The manifest migrations block. The catalogue keeps its own check and messages (_catalog)."""
    migrations = _object(m["migrations"], ("leaves", "from_versions", "labels"), ("leaves", "from_versions"))
    if not isinstance(migrations["leaves"], list) or len(set(migrations["leaves"])) != len(migrations["leaves"]):
        _fail("Migration leaves must be unique names.")
    for leaf in migrations["leaves"]:
        if not isinstance(leaf, str) or not re.fullmatch(r"[A-Za-z0-9_]+", leaf):
            _fail("Invalid migration leaf.")
    _specifier(migrations["from_versions"])
    if "labels" in migrations:
        if not isinstance(migrations["labels"], list) or len(set(migrations["labels"])) != len(migrations["labels"]):
            _fail("Invalid migration labels.")
        for label in migrations["labels"]:
            _identifier(label)
        if m["kind"] in APPLICATION_KINDS and migrations["labels"] != [m["app_label"]]:
            _fail("Application migrations must use its own label.")


def _validate_lifecycle(m):
    lifecycle = _object(
        m["lifecycle"], ("data_retention", "maintenance", "backup"), ("data_retention", "maintenance", "backup")
    )
    if (
        lifecycle["data_retention"] != "retain"
        or lifecycle["maintenance"] is not True
        or lifecycle["backup"] is not True
    ):
        _fail("Release requires maintenance, backup and retained data.")


def _validate_file(m, item, seen):
    _object(item, ("path", "size", "sha256", "role"), ("path", "size", "sha256", "role"))
    path = _path(item["path"])
    allowed_roots = ("app/", "wheels/") if m["kind"] == "app" else ("wheels/",)
    if not path.startswith(allowed_roots) or path.casefold() in seen:
        _fail("Unowned or duplicate file inventory path.")
    if path in ("app/manifest.json", "app/manifest.sig"):
        _fail("Canonical manifest/signature are installer-owned, not payload files.")
    if m["kind"] == "app" and path.startswith(("app/templates/", "app/static/")):
        prefix = "/".join(path.split("/")[:2]) + "/" + m["app_label"] + "/"
        if not path.startswith(prefix):
            _fail("Static/template files must belong to the app label namespace.")
    seen.add(path.casefold())
    if (
        type(item["size"]) is not int
        or not 0 <= item["size"] <= limits.MAX_FILE_BYTES
        or not isinstance(item["sha256"], str)
        or not SHA256.fullmatch(item["sha256"])
    ):
        _fail("Invalid file size/hash.")
    roles = (
        ("code", "migration", "template", "static", "documentation", "data") if path.startswith("app/") else ("wheel",)
    )
    if item["role"] not in roles:
        _fail("Invalid file role.")


def _validate_files(m):
    if not isinstance(m["files"], list) or len(m["files"]) > limits.MAX_MEMBERS - 2:
        _fail("Invalid file inventory.")
    seen = set()
    for item in m["files"]:
        _validate_file(m, item, seen)


def _validate_python_dependency(m, dependency, wheel_paths, wheel_names):
    _object(dependency, ("name", "version", "wheel", "sha256", "marker"), ("name", "version", "wheel", "sha256"))
    path = _path(dependency["wheel"])
    if not path.startswith("wheels/") or "/" in path[7:] or not path.endswith(".whl") or path in wheel_paths:
        _fail("Only individually declared wheels are supported.")
    try:
        name, version, _, _ = parse_wheel_filename(Path(path).name)
    except Exception as exc:
        raise PackageError("Invalid wheel filename.") from exc
    if (
        canonicalize_name(_string(dependency["name"])) != name
        or version != _python_version(dependency["version"])
        or name in wheel_names
    ):
        _fail("Wheel identity or version mismatch.")
    if "marker" in dependency:
        try:
            Marker(dependency["marker"])
        except (InvalidMarker, TypeError) as exc:
            raise PackageError("Invalid wheel marker.") from exc
    wheel_paths.add(path)
    wheel_names.add(name)
    item = next((i for i in m["files"] if i["path"] == path), None)
    if item is None or item["sha256"] != dependency["sha256"]:
        _fail("Wheel inventory/hash mismatch.")
    if m["kind"] == "core" and name == "django-skeleton":
        if Version(dependency["version"]) != Version(m["version"]) or "marker" in dependency:
            _fail("Core manifest version must match its unconditional core wheel.")


def _validate_python_dependencies(m):
    if not isinstance(m["python_dependencies"], list):
        _fail("Python dependencies require a wheel list.")
    wheel_paths = set()
    wheel_names = set()
    for dependency in m["python_dependencies"]:
        _validate_python_dependency(m, dependency, wheel_paths, wheel_names)
    if {i["path"] for i in m["files"] if i["path"].startswith("wheels/")} != wheel_paths:
        _fail("Unlisted wheel dependency.")
    if m["kind"] == "core" and "django-skeleton" not in wheel_names and m["files"]:
        _fail("Core artifact requires its core wheel.")


def _validate_manifest(value):
    """The blocks run in the order they always did: it decides which error a manifest reports first."""
    m = _validate_header(value)
    if m["kind"] in APPLICATION_KINDS:
        label, prefix, bundled = _validate_application_identity(m)
        _validate_runtime(m, label, prefix, bundled)
    else:
        _validate_core_identity(m)
    _validate_relations(m)
    _validate_migrations(m)
    _validate_lifecycle(m)
    _validate_files(m)
    _validate_python_dependencies(m)
    for optional in ("release_notes", "build"):
        if optional in m:
            _string(m[optional])
    return m


def validate_manifest(value):
    """Validate source or inventoried manifest; return canonical independent data."""
    try:
        return _validate_manifest(value)
    except PackageError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise PackageError("Invalid manifest field type or value.") from exc


def load_manifest(path):
    path = Path(path)
    if path.stat().st_size > limits.MAX_METADATA_BYTES:
        _fail("Manifest is too large.")
    return validate_manifest(_json(path.read_bytes()))


def required_documents(manifest):
    """Document -> required headings; the service API reference once runtime.service_api exists."""
    documents = dict(DOCUMENT_HEADINGS)
    if "service_api" in manifest.get("runtime", {}):
        documents[SERVICE_API_DOCUMENT] = SERVICE_API_HEADINGS
    return documents
