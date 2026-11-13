"""skeleton.system.packages stays the stable facade of the split packaging modules (#315)."""

import subprocess
import sys

from skeleton.system import packages

# Names other modules, the agent, derived projects and tests import from the facade.
SHARED_NAMES = (
    "PackageError",
    "VerifiedPackage",
    "canonical_json",
    "validate_manifest",
    "load_manifest",
    "manifest_template",
    "default_service_class",
    "validate_package",
    "inspect_package",
    "extract_package",
    "build_package",
    "build_artifact",
    "sign_package",
    "initialize_keys",
    "load_trust_store",
    "validate_wheel_set",
    "validate_dependency_graph",
    "build_catalog",
    "validate_catalog",
    "catalog_entry",
    "doctor",
    "static_migration_leaves",
    "check_migration_leaves",
    "required_documents",
    "configure_parser",
    "run_command",
    "main",
    "BUNDLED_KIND",
    "BUNDLED_PACKAGE",
    "BUNDLED_LABEL_PREFIX",
    "DOCUMENT_HEADINGS",
    "SERVICE_API_DOCUMENT",
    "SERVICE_API_HEADINGS",
    "SERVICE_API_CORE_MINIMUM",
    "SUGGESTIBLE_PROBLEM_CODES",
    "MAX_ARCHIVE_BYTES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "MAX_MEMBERS",
    "MAX_WHEELHOUSE_MEMBERS",
    "MAX_WHEEL_MEMBERS",
    "MAX_METADATA_BYTES",
    "MAX_EXPANSION_RATIO",
    "MAX_SERVICE_CLASSES",
    # Private names used outside the package (app_scaffold, dependencies, distribution, tests).
    "_identifier",
    "_stable_id",
    "_marker_active",
    "_target_environment",
    "_catalog",
    "_validate_archive",
    "_protected_snapshot",
    "_zip_members",
    "_read_member",
)


def test_facade_keeps_every_public_and_shared_private_name():
    assert [name for name in SHARED_NAMES if not hasattr(packages, name)] == []


def test_facade_all_lists_only_public_names():
    assert packages.__all__ == sorted(packages.__all__)
    assert not [name for name in packages.__all__ if name.startswith("_")]
    assert all(hasattr(packages, name) for name in packages.__all__)
    assert {name for name in SHARED_NAMES if not name.startswith("_")} <= set(packages.__all__)


def test_packaging_modules_do_not_import_django():
    code = (
        "import sys, pkgutil, importlib, skeleton.system.packaging as root; "
        "[importlib.import_module('skeleton.system.packaging.' + m.name) for m in pkgutil.iter_modules(root.__path__)]; "
        "import skeleton.system.packages, skeleton.system.dependencies; "
        "assert 'django' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
