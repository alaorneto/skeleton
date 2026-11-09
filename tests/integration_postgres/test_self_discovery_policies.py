"""The schema check pins the self-discovery policy inventory of the frozen specification (#18)."""

import pytest

from skeleton.system.checks import check_deployed_schema
from tools.integration_database import disposable_database_pair


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


def test_membership_uses_self_policies(pristine):
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT policyname FROM pg_policies WHERE schemaname='skeleton' AND tablename='sk_tenancy_membership'"
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "skeleton_self_select",
            "skeleton_self_insert",
            "skeleton_self_update",
            "skeleton_self_delete",
        }


def test_missing_self_policy_fails_schema_check(pristine):
    _owner_execute("DROP POLICY {} ON skeleton.{}", "skeleton_self_select", "sk_tenancy_membership")
    assert any("sk_tenancy_membership" in error for error in check_deployed_schema())


def test_extra_policy_fails_schema_check(pristine):
    _owner_execute(
        "CREATE POLICY {} ON skeleton.{} FOR SELECT TO skeleton_runtime USING (true)",
        "fixture_extra_discovery",
        "sk_tenancy_membership",
    )
    assert any("sk_tenancy_membership" in error for error in check_deployed_schema())


def test_widened_self_select_fails_schema_check(pristine):
    _owner_execute("DROP POLICY {} ON skeleton.{}", "skeleton_self_select", "sk_tenancy_membership")
    _owner_execute(
        "CREATE POLICY {} ON skeleton.{} FOR SELECT TO skeleton_runtime USING (true)",
        "skeleton_self_select",
        "sk_tenancy_membership",
    )
    assert any("skeleton_self_select" in error for error in check_deployed_schema())
