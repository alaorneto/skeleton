"""skeleton.system.checks is a package, and every name the core or derived projects import stays there (#252)."""

import importlib

import pytest

PUBLIC = (
    "check_deployed_schema",
    "frozen_operations",
    "_frozen_operations",
    "record_contract_checks",
    "service_api_checks",
    "import_boundary_checks",
    "extension_consistency_checks",
    "undeclared_migration_dependencies",
    "undeclared_relations",
    "stored_secret_key_checks",
    "stored_secrets_without_keys",
    "encrypted_secret_checks",
    "postgresql_only_checks",
    # Kept for derived projects.
    "expected_policies",
    "shared_cache_checks",
    "KEYRINGS",
    "POSTGRESQL_ENGINE",
    "_canonical",
    "_sql_tokens",
    "_trigger_tokens",
    "_expected_sql",
    "_check_lifecycle",
    "_boundary_providers",
)


@pytest.mark.parametrize("name", PUBLIC)
def test_public_names_stay_importable(name):
    checks = importlib.import_module("skeleton.system.checks")
    assert hasattr(checks, name), name


def test_frozen_operations_keeps_its_old_name():
    from skeleton.system.checks import _expected_sql, _frozen_operations, expected_sql, frozen_operations

    assert frozen_operations is _frozen_operations and expected_sql is _expected_sql


def test_the_package_replaces_the_module():
    from pathlib import Path

    import skeleton.system.checks as checks

    root = Path(checks.__file__).parent
    assert Path(checks.__file__).name == "__init__.py" and not (root.parent / "checks.py").exists()
    assert {path.stem for path in root.glob("*.py")} >= {
        "contract",
        "environment",
        "service_api",
        "extensions",
        "frozen",
        "sqltext",
        "schema",
        "encryption",
    }
