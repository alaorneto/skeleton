"""The deployed-schema check pins the infrastructure of the admission tables (#26, #27, #28, #29)."""

import pytest

from skeleton.system.checks import check_deployed_schema
from tools.integration_database import disposable_database_pair

POLICY = "sk_tenancy_organizationadmissionpolicy"
REQUEST = "sk_tenancy_joinrequest"
DIRECTORY = "sk_tenancy_organizationdirectoryentry"
LINK = "sk_tenancy_joinlink"
DOMAIN = "sk_tenancy_verifieddomain"
CLAIM = "sk_tenancy_domainclaim"
IDP = "sk_accounts_identityprovider"
IDP_ADMISSION = "sk_tenancy_identityprovideradmission"


def _owner_execute(statement, *identifiers):
    import psycopg
    from psycopg import sql

    pair = disposable_database_pair()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        owner.execute(sql.SQL(statement).format(*map(sql.Identifier, identifiers)))


def _query(statement, *params):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(statement, list(params))
        return cursor.fetchall()


@pytest.fixture
def pristine(skeleton_db):
    from django.db import connection

    assert connection.vendor == "postgresql", "TEST_DATABASE_URL must select a real PostgreSQL fixture."
    assert check_deployed_schema() == []


def _rls(table):
    return _query(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='skeleton' AND c.relname=%s",
        table,
    )[0]


def _policies(table):
    return {
        row[0]
        for row in _query("SELECT policyname FROM pg_policies WHERE schemaname='skeleton' AND tablename=%s", table)
    }


def _triggers(table):
    return {
        row[0]
        for row in _query(
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname=%s AND NOT tgisinternal",
            table,
        )
    }


def test_policy_is_tenant_scoped_and_requests_have_self_discovery(pristine):
    for table in (POLICY, LINK, DOMAIN, IDP_ADMISSION):
        assert _rls(table) == (True, True)
        assert "skeleton_tenant" in _policies(table)
    assert _rls(REQUEST) == (True, True)
    request_policies = _policies(REQUEST)
    assert "skeleton_tenant" not in request_policies
    assert request_policies and all(name.startswith("skeleton_self_") for name in request_policies)
    # The directory index is global: no tenant policy, still guarded and audited.
    assert _rls(DIRECTORY) == (False, False)
    assert _rls(CLAIM) == (False, False)
    assert _rls(IDP) == (False, False)
    for table in (POLICY, REQUEST, DIRECTORY, LINK, DOMAIN, CLAIM, IDP, IDP_ADMISSION):
        assert {table + "_sk_guard", table + "_sk_audit"} <= _triggers(table)


@pytest.mark.parametrize("table", [POLICY, REQUEST, DIRECTORY, LINK, DOMAIN, CLAIM, IDP, IDP_ADMISSION])
@pytest.mark.parametrize("kind", ["guard", "audit"])
def test_missing_trigger_fails_schema_check(pristine, table, kind):
    _owner_execute("DROP TRIGGER {} ON skeleton.{}", f"{table}_sk_{kind}", table)
    assert any(table in error for error in check_deployed_schema())


def test_missing_tenant_policy_fails_schema_check(pristine):
    _owner_execute("DROP POLICY {} ON skeleton.{}", "skeleton_tenant", POLICY)
    assert any(POLICY in error for error in check_deployed_schema())


def test_missing_self_discovery_policy_fails_schema_check(pristine):
    name = sorted(_policies(REQUEST))[0]
    _owner_execute("DROP POLICY {} ON skeleton.{}", name, REQUEST)
    assert any(REQUEST in error for error in check_deployed_schema())
