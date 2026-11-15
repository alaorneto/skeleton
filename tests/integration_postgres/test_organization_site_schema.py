"""The deployed-schema check pins the infrastructure of the organization site tables (#480, #488)."""

import pytest

from skeleton.system.checks import check_deployed_schema
from tests.integration_postgres.test_admission_schema import _owner_execute, _policies, _rls, _triggers

SITE = "sk_tenancy_organizationsite"
SETTINGS = "sk_tenancy_organizationsettings"
DOMAIN = "sk_tenancy_sitedomain"
CLAIM = "sk_tenancy_sitedomainclaim"
GLOBAL_TABLES = (SITE, CLAIM)
TENANT_TABLES = (SETTINGS, DOMAIN)


@pytest.fixture
def pristine(skeleton_db):
    from django.db import connection

    assert connection.vendor == "postgresql", "TEST_DATABASE_URL must select a real PostgreSQL fixture."
    assert check_deployed_schema() == []


def test_site_tables_have_their_infrastructure(pristine):
    for table in GLOBAL_TABLES:
        assert _rls(table) == (False, False)  # Global index: read before any tenant exists.
    for table in TENANT_TABLES:
        assert _rls(table) == (True, True)
        assert "skeleton_tenant" in _policies(table)
    for table in (*GLOBAL_TABLES, *TENANT_TABLES):
        assert {table + "_sk_guard", table + "_sk_audit"} <= _triggers(table)


@pytest.mark.parametrize("table", [*GLOBAL_TABLES, *TENANT_TABLES])
@pytest.mark.parametrize("kind", ["guard", "audit"])
def test_missing_trigger_fails_schema_check(pristine, table, kind):
    _owner_execute("DROP TRIGGER {} ON skeleton.{}", f"{table}_sk_{kind}", table)
    assert any(table in error for error in check_deployed_schema())
