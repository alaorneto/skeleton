"""The deployed-schema check pins the infrastructure of ``sk_transfer_transferjob`` (#182)."""

import pytest

from skeleton.system.checks import check_deployed_schema
from tools.integration_database import disposable_database_pair

TABLE = "sk_transfer_transferjob"


def _owner_execute(statement, *identifiers):
    import psycopg
    from psycopg import sql

    pair = disposable_database_pair()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        owner.execute(sql.SQL(statement).format(*map(sql.Identifier, identifiers)))


@pytest.fixture
def pristine(skeleton_db):
    from django.db import connection

    assert connection.vendor == "postgresql", "TEST_DATABASE_URL must select a real PostgreSQL fixture."
    assert check_deployed_schema() == []


def test_tenant_policy_rls_and_triggers_are_installed(pristine):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='skeleton' AND c.relname=%s",
            [TABLE],
        )
        assert cursor.fetchone() == (True, True)
        cursor.execute("SELECT policyname FROM pg_policies WHERE schemaname='skeleton' AND tablename=%s", [TABLE])
        assert "skeleton_tenant" in {row[0] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname=%s AND NOT tgisinternal",
            [TABLE],
        )
        assert {TABLE + "_sk_guard"} <= {row[0] for row in cursor.fetchall()}


@pytest.mark.parametrize("kind", ["guard"])
def test_missing_trigger_fails_schema_check(pristine, kind):
    _owner_execute("DROP TRIGGER {} ON skeleton.{}", f"{TABLE}_sk_{kind}", TABLE)
    assert any(TABLE in error for error in check_deployed_schema())


def test_missing_tenant_policy_fails_schema_check(pristine):
    _owner_execute("DROP POLICY {} ON skeleton.{}", "skeleton_tenant", TABLE)
    assert any(TABLE in error for error in check_deployed_schema())
