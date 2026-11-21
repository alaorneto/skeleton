"""Frozen specification and guard of a global table declared by an app (#576), without database."""

import pytest
from django.db import models
from django.test.utils import isolate_apps

from skeleton.common.context import is_app_global_table
from skeleton.common.migration_operations import InstallRecordInfrastructure, build_infrastructure_specification
from skeleton.common.models import SystemRecord
from skeleton.system.registry import TableSpec

GUARD = "IF current_setting('skeleton.system',true) IS DISTINCT FROM 'on'"


class Editor:
    def quote_name(self, name):
        return f'"{name}"'


def make(app_label, name, **fields):
    attrs = {
        "__module__": __name__,
        "code": models.CharField(max_length=10),
        "Meta": type("Meta", (), {"app_label": app_label}),
        **fields,
    }
    return type(name, (SystemRecord,), attrs)


@isolate_apps("skeleton.system.apps.SystemConfig")
def test_only_global_tables_of_apps_freeze_system_writes():
    app_global = make("example_notes", "CatalogEntry")
    core_global = make("sk_system", "CoreEntry")
    # The ``organization`` field is what makes a table organizational (is_tenant_model).
    tenant = make("example_notes", "TenantEntry", organization=models.ForeignKey(core_global, on_delete=models.PROTECT))
    cases = (
        (app_global, "global", True),
        (core_global, "global", False),
        (tenant, "organization", False),
    )
    for model, scope, expected in cases:
        assert is_app_global_table(model) is expected, model
        spec = TableSpec(model=model, description="x", scope=scope, read_fields=("code",))
        specification = build_infrastructure_specification(model, spec)
        assert ("writes" in specification) is expected, model
        body = InstallRecordInfrastructure(model._meta.app_label, model._meta.model_name, specification)._guard_body(
            Editor(), model
        )
        assert (GUARD in body) is expected, model
        if expected:
            assert specification["writes"] == "system"
            assert (
                body.startswith(GUARD) and "NULLIF(current_setting('skeleton.tenant_id',true),'') IS NOT NULL" in body
            )
        else:
            assert body.startswith("IF TG_OP='DELETE'")  # byte for byte the historical body


def test_a_frozen_writes_value_other_than_system_is_refused():
    with pytest.raises(ValueError, match="writes"):
        InstallRecordInfrastructure("example_notes", "catalogentry", {"scope": "global", "writes": "anyone"})
