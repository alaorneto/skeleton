"""Infrastructure identifiers never exceed PostgreSQL's 63 bytes (#238)."""

from types import SimpleNamespace

import pytest
from django.apps import apps
from django.db import models
from django.test.utils import isolate_apps

from skeleton.common.migration_operations import (
    LEGACY_LONG_IDENTIFIERS,
    MAX_IDENTIFIER_BYTES,
    InstallRecordInfrastructure,
    infrastructure_identifiers,
    oversized_identifiers,
)
from skeleton.common.models import OperationalRecord, OrganizationRecord, SystemRecord

SPEC = {"scope": "global", "sensitive_fields": [], "columns": []}


def _model(isolated_name, table, **fields):
    attrs = {
        "__module__": __name__,
        "Meta": type("Meta", (SystemRecord.Meta,), {"app_label": "sk_system", "db_table": table}),
    }
    attrs.update(fields)
    return type(isolated_name, (SystemRecord,), attrs)


@isolate_apps("skeleton.system.apps.SystemConfig")
def test_short_tables_have_no_oversized_identifiers():
    model = _model("ShortProbe", "sk_test_short")
    assert oversized_identifiers(model) == []
    assert infrastructure_identifiers(model)[:4] == [
        "sk_test_short_sk_guard",
        "sk_test_short_sk_audit",
        "sk_test_short_sk_guard_fn",
        "sk_test_short_sk_audit_fn",
    ]


@isolate_apps("skeleton.system.apps.SystemConfig")
def test_long_table_reports_every_generated_name():
    table = "t" * 60
    model = _model("LongProbe", table)
    reported = dict(oversized_identifiers(model))
    for suffix in ("_sk_guard", "_sk_audit", "_sk_guard_fn", "_sk_audit_fn"):
        assert reported[table + suffix] == len(table + suffix)


@isolate_apps("skeleton.system.apps.SystemConfig")
def test_long_foreign_key_column_reports_constraint_and_index():
    table = "t" * 40
    column = "c" * 20
    model = _model(
        "RefProbe",
        table,
        role=models.ForeignKey(apps.get_model("sk_access", "Role"), on_delete=models.PROTECT, db_column=column),
    )
    names = {name for name, _ in oversized_identifiers(model)}
    assert {f"{table}_sk_org_{column}", f"{table}_sk_idx_{column}"} <= names


@isolate_apps("skeleton.system.apps.SystemConfig")
def test_non_ascii_table_name_is_measured_in_bytes():
    table = "é" * 30  # 30 characters, 60 bytes.
    model = _model("AccentProbe", table)
    assert all(len(name) <= MAX_IDENTIFIER_BYTES for name in infrastructure_identifiers(model)[:2])
    assert oversized_identifiers(model)


@isolate_apps("skeleton.system.apps.SystemConfig")
def test_database_forwards_refuses_before_any_ddl():
    model = _model("RefusedProbe", "t" * 60)
    statements = []
    editor = SimpleNamespace(
        connection=SimpleNamespace(vendor="postgresql", alias="default"),
        quote_name=lambda name: f'"{name}"',
        execute=statements.append,
    )
    state = SimpleNamespace(apps=SimpleNamespace(get_model=lambda app, name: model))
    operation = InstallRecordInfrastructure("sk_system", "refusedprobe", SPEC)
    operation.allow_migrate_model = lambda alias, model: True
    with pytest.raises(ValueError, match="63 bytes"):
        operation.database_forwards("sk_system", editor, state, state)
    assert statements == []


def test_check_reports_e033():
    from skeleton.system.checks import infrastructure_identifier_checks

    with isolate_apps("skeleton.system.apps.SystemConfig") as isolated:
        _model("CheckProbe", "t" * 60)
        errors = infrastructure_identifier_checks(apps_registry=isolated)
    assert errors and {error.id for error in errors} == {"skeleton.E033"}


def test_core_models_generate_no_oversized_identifier():
    owned = [m for m in apps.get_models() if issubclass(m, SystemRecord | OperationalRecord) and not m._meta.proxy]
    assert owned
    assert [(model._meta.label, oversized_identifiers(model)) for model in owned if oversized_identifiers(model)] == []


def test_legacy_long_identifiers_stay_unique_once_truncated():
    owned = [m for m in apps.get_models() if issubclass(m, SystemRecord | OperationalRecord) and not m._meta.proxy]
    generated = [name for model in owned for name in infrastructure_identifiers(model)]
    assert LEGACY_LONG_IDENTIFIERS <= set(generated)
    truncated = [name.encode()[:MAX_IDENTIFIER_BYTES] for name in generated]
    assert len(truncated) == len(set(truncated))
    assert issubclass(apps.get_model("sk_tenancy", "OrganizationAdmissionPolicy"), OrganizationRecord)
