"""Closed, signed release artifacts. This module never imports Django or app code.

Trust store: publisher/key-id -> Ed25519PublicKey, public PEM text/path, or
{'public_key': public PEM text/path/object, 'publisher': same publisher/key-id}.
An artifact cannot supply its own trust anchor. Python payload remains privileged.
"""

# ruff: noqa: F401 - the facade re-exports every name of the split package (#315).
import json

# fmt: off
# isort: off
from .packaging.errors import (
    PackageError, _fail,
)
from .packaging.limits import (
    MAX_ARCHIVE_BYTES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_MEMBERS, MAX_WHEELHOUSE_MEMBERS, MAX_WHEEL_MEMBERS,
    MAX_METADATA_BYTES, MAX_EXPANSION_RATIO, BUNDLED_KIND, BUNDLED_PACKAGE, BUNDLED_LABEL_PREFIX, RESERVED_NAMES,
    RESERVED_LAUNCHERS, INTERPRETER_LAUNCHER, IDENTIFIER, STABLE_ID, SHA256, SERVICE_CLASS, MAX_SERVICE_CLASSES,
    SERVICE_API_CORE_MINIMUM, SERVICE_API_DOCUMENT, SERVICE_API_HEADINGS, CATALOG_VERSIONS,
    BUILD_EXCLUDED_DIRECTORIES, DOCUMENT_HEADINGS, SUGGESTIBLE_PROBLEM_CODES,
)
from .packaging.primitives import (
    canonical_json, _object, _string, _identifier, _stable_id, _path, _version, _python_version, _specifier,
    _compatibility, _core_floor_at_least, _json, _timestamp,
)
from .packaging.signing import (
    _public_key, _trusted_key, _private_key, _verify, initialize_keys, load_trust_store,
)
from .packaging.manifest import (
    _validate_service_api, _validate_manifest, validate_manifest, load_manifest, required_documents,
)
from .packaging.template import (
    default_service_class, manifest_template,
)
from .packaging.zipio import (
    _zip_members, _read_member, _protected_snapshot,
)
from .packaging.wheels import (
    _target_environment, _marker_active, _wheel_scripts, _wheel, _claim_wheel, validate_wheel_set,
)
from .packaging.archive import (
    VerifiedPackage, _module_path, _documentation, _entrypoint, _validate_archive, validate_package, inspect_package,
    extract_package,
)
from .packaging.build import (
    _role, build_artifact, _literal_dependencies, static_migration_leaves, check_migration_leaves,
    _scan_application_root, build_package, sign_package,
)
from .packaging.catalog import (
    _catalog, build_catalog, validate_catalog, catalog_entry,
)
from .packaging.doctor import (
    _doctor_dependencies, _doctor_import_boundary, doctor, validate_dependency_graph,
)
from .packaging.cli import (
    configure_parser, run_command,
)
# isort: on
# fmt: on

# fmt: off
__all__ = [
    "BUILD_EXCLUDED_DIRECTORIES", "BUNDLED_KIND", "BUNDLED_LABEL_PREFIX", "BUNDLED_PACKAGE", "CATALOG_VERSIONS",
    "DOCUMENT_HEADINGS", "IDENTIFIER", "INTERPRETER_LAUNCHER", "MAX_ARCHIVE_BYTES", "MAX_EXPANSION_RATIO",
    "MAX_FILE_BYTES", "MAX_MEMBERS", "MAX_METADATA_BYTES", "MAX_SERVICE_CLASSES", "MAX_TOTAL_BYTES",
    "MAX_WHEELHOUSE_MEMBERS", "MAX_WHEEL_MEMBERS", "PackageError", "RESERVED_LAUNCHERS", "RESERVED_NAMES",
    "SERVICE_API_CORE_MINIMUM", "SERVICE_API_DOCUMENT", "SERVICE_API_HEADINGS", "SERVICE_CLASS", "SHA256",
    "STABLE_ID", "SUGGESTIBLE_PROBLEM_CODES", "VerifiedPackage", "build_artifact", "build_catalog", "build_package",
    "canonical_json", "catalog_entry", "check_migration_leaves", "configure_parser", "default_service_class",
    "doctor", "extract_package", "initialize_keys", "inspect_package", "load_manifest", "load_trust_store", "main",
    "manifest_template", "required_documents", "run_command", "sign_package", "static_migration_leaves",
    "validate_catalog", "validate_dependency_graph", "validate_manifest", "validate_package", "validate_wheel_set",
]
# fmt: on


def main(argv=None):
    import argparse

    parser = configure_parser(argparse.ArgumentParser(description=__doc__))
    options = vars(parser.parse_args(argv))
    try:
        result = run_command(options)
    except (PackageError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if options["operation"] == "doctor" and not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
