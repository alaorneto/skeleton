"""The record gateway lives in skeleton.records; the historical paths re-export the same objects (#301)."""

import importlib

import pytest

GATEWAY = (
    "ACCESS_FIELDS",
    "SYSTEM_FIELDS",
    "assert_fields",
    "change_record_access",
    "coerce_fields",
    "create_record",
    "delete_record",
    "exposed_spec",
    "filter_records",
    "readable_specs",
    "record_data",
    "record_queryset",
    "records_data",
    "require_access_management",
    "restore_record",
    "update_record",
)
PICKER = (
    "ACCESS_REFERENCE_FIELDS",
    "CORE_REFERENCE_LABELS",
    "label_field",
    "reference_label",
    "reference_options",
    "reference_queryset",
    "reference_request",
    "target_queryset",
)
PAIRS = (
    *(("skeleton.web.services", "skeleton.records.services", name) for name in GATEWAY),
    *(("skeleton.web.references", "skeleton.records.references", name) for name in PICKER),
    *(
        ("skeleton.api.serializers", "skeleton.records.dedicated", name)
        for name in ("dedicated_input", "dedicated_data", "dedicated_required", "DEDICATED_REQUIRED", "DEDICATED_EXTRA")
    ),
    *(
        ("skeleton.api.serializers", "skeleton.access.resources", name)
        for name in ("DEDICATED", "LINKS", "dedicated_model")
    ),
    ("skeleton.api.serializers", "skeleton.audit.presentation", "audit_data"),
)


@pytest.mark.parametrize(("old", "new", "name"), PAIRS, ids=lambda value: value)
def test_historical_paths_reexport_the_same_objects(old, new, name):
    assert getattr(importlib.import_module(old), name) is getattr(importlib.import_module(new), name)


def test_records_is_imported_only_above_the_kernel():
    """Negative: no kernel package imports the gateway (it sits above access and system)."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "skeleton"
    offenders = []
    for package in ("common", "tenancy", "access", "system", "accounts", "audit"):
        for path in (root / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                module = getattr(node, "module", None) if isinstance(node, ast.ImportFrom) else None
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [module or ""]
                if any(name.startswith("skeleton.records") for name in names):
                    offenders.append(path.relative_to(root).as_posix())
    assert offenders == []
