"""The schema restored for each test is the schema migrate produced, not an approximation.

A template that lost a policy, trigger, grant or RLS flag would let the runtime-security
tests pass for the wrong reason, so the restored catalog is compared with the migrated one.
"""

import psycopg
import pytest
from django.db import connection
from psycopg import sql

from skeleton.access.models import Role
from skeleton.system.checks import check_deployed_schema
from tools.integration_database import disposable_database_pair, schema_fingerprint

PROBE_APP = "fixture_isolation_probe"


@pytest.fixture
def template(skeleton_db, postgres_template):
    if postgres_template is None:
        pytest.skip("SKELETON_TEST_POSTGRES_TEMPLATE=0: each test migrates, there is no template.")
    assert connection.vendor == "postgresql", "The schema template exists only for real PostgreSQL."
    return postgres_template


def test_restored_schema_matches_migrated_schema(template):
    restored = schema_fingerprint(disposable_database_pair())
    for section, expected in template.fingerprint.items():
        assert restored[section] == expected, f"Restored schema differs from migrated schema in {section}."
    assert restored["policies"] and restored["triggers"], "Fingerprint must cover RLS policies and triggers."
    assert check_deployed_schema() == []


def test_runtime_identity_is_unchanged_by_restore(template):
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
        assert cursor.fetchone() == ("skeleton_runtime", False, False)


def _forced_rls_table(owner):
    row = owner.execute(
        "SELECT relname FROM pg_class WHERE relnamespace='skeleton'::regnamespace AND relforcerowsecurity ORDER BY 1 LIMIT 1"
    ).fetchone()
    assert row, "Migrated schema must have tables with FORCE ROW LEVEL SECURITY."
    return row[0]


def _first_policy(owner):
    row = owner.execute(
        "SELECT policyname, tablename FROM pg_policies WHERE schemaname='skeleton' ORDER BY 2, 1 LIMIT 1"
    ).fetchone()
    assert row, "Migrated schema must have RLS policies."
    return row


@pytest.mark.parametrize("mutation", ["policy", "trigger", "grant", "force_rls", "data"])
def test_fingerprint_detects_missing_security_object(template, mutation):
    pair = disposable_database_pair()
    table = Role._meta.db_table
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        if mutation == "policy":
            policy, policy_table = _first_policy(owner)
            owner.execute(
                sql.SQL("DROP POLICY {} ON skeleton.{}").format(sql.Identifier(policy), sql.Identifier(policy_table))
            )
        elif mutation == "trigger":
            owner.execute(
                sql.SQL("DROP TRIGGER {} ON skeleton.{}").format(
                    sql.Identifier(table + "_sk_audit"), sql.Identifier(table)
                )
            )
        elif mutation == "grant":
            owner.execute(sql.SQL("REVOKE SELECT ON skeleton.{} FROM skeleton_runtime").format(sql.Identifier(table)))
        elif mutation == "force_rls":
            owner.execute(
                sql.SQL("ALTER TABLE skeleton.{} NO FORCE ROW LEVEL SECURITY").format(
                    sql.Identifier(_forced_rls_table(owner))
                )
            )
        else:
            owner.execute(
                "DELETE FROM skeleton.django_migrations WHERE id=(SELECT max(id) FROM skeleton.django_migrations)"
            )
    assert schema_fingerprint(pair) != template.fingerprint


def _probe_rows(owner):
    return owner.execute("SELECT count(*) FROM skeleton.django_migrations WHERE app=%s", (PROBE_APP,)).fetchone()[0]


def test_isolation_probe_writes_committed_row(template):
    pair = disposable_database_pair()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        owner.execute(
            "INSERT INTO skeleton.django_migrations (app, name, applied) VALUES (%s, '0001_probe', now())",
            (PROBE_APP,),
        )
        assert _probe_rows(owner) == 1


def test_isolation_probe_row_is_gone_in_next_test(template):
    # Runs after the writer above (pytest keeps definition order): each test gets a recreated schema.
    pair = disposable_database_pair()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        assert _probe_rows(owner) == 0


def test_session_migrates_once_and_restores_per_test(template, postgres_schema_stats):
    assert postgres_schema_stats["migrate"] == 1
    assert postgres_schema_stats["restore"] >= 1
