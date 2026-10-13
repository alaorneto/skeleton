"""Historical schema assurance, with owner used only for isolated DDL faults."""
import psycopg
from psycopg import sql
import pytest
from django.db import connection

from skeleton.access.models import MemberRole, Role
from skeleton.system.checks import check_deployed_schema
from skeleton.tenancy.models import Membership
from tools.integration_database import disposable_database_pair


@pytest.fixture
def pristine_schema(skeleton_db):
    pair = disposable_database_pair()
    assert connection.vendor == "postgresql", "These regressions require real PostgreSQL."
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
        assert cursor.fetchone() == ("skeleton_runtime", False, False)
    assert check_deployed_schema() == [], "Migrated fixture must pass before isolated DDL tampering."
    return pair


@pytest.mark.parametrize("kind", ["guard", "audit"])
@pytest.mark.parametrize("mutation", ["function", "events", "timing", "when", "arguments", "statement"])
def test_rd03_trigger_binding_matches_historical_definition(pristine_schema, kind, mutation):
    table = Role._meta.db_table
    trigger = table + "_sk_" + kind
    function = table + "_sk_" + kind + "_fn"
    timing = "BEFORE" if kind == "guard" else "AFTER"
    events, level, condition, arguments = "INSERT OR UPDATE OR DELETE", "ROW", "", ""
    with psycopg.connect(pristine_schema.owner, autocommit=True) as owner:
        if mutation == "function":
            # The historical function stays intact; only tgfoid is redirected.
            function = "fixture_unprotected_trigger"
            owner.execute(sql.SQL("CREATE FUNCTION skeleton.{}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN COALESCE(NEW,OLD); END $$").format(sql.Identifier(function)))
            owner.execute(sql.SQL("REVOKE ALL ON FUNCTION skeleton.{}() FROM PUBLIC").format(sql.Identifier(function)))
        elif mutation == "events":
            events = "INSERT"
        elif mutation == "timing":
            timing = "AFTER" if kind == "guard" else "BEFORE"
        elif mutation == "when":
            condition = "WHEN (false)"
        elif mutation == "arguments":
            arguments = "'fixture-disabled'"
        elif mutation == "statement":
            level = "STATEMENT"
        owner.execute(sql.SQL("DROP TRIGGER {} ON skeleton.{}").format(sql.Identifier(trigger), sql.Identifier(table)))
        owner.execute(sql.SQL("CREATE TRIGGER {} {} {} ON skeleton.{} FOR EACH {} {} EXECUTE FUNCTION skeleton.{}({})").format(
            sql.Identifier(trigger), sql.SQL(timing), sql.SQL(events), sql.Identifier(table),
            sql.SQL(level), sql.SQL(condition), sql.Identifier(function), sql.SQL(arguments),
        ))
    errors = check_deployed_schema()
    assert any(trigger in error and "trigger definition" in error for error in errors), errors


@pytest.mark.parametrize("mutation", [
    "missing", "body", "search_path", "security", "owner", "public_execute",
    "runtime_execute", "grant_option", "signature", "return_type",
])
def test_rd04_lifecycle_guard_matches_historical_inventory(pristine_schema, mutation):
    function = "skeleton.assert_user_deactivation(uuid)"
    with psycopg.connect(pristine_schema.owner, autocommit=True) as owner:
        if mutation == "missing":
            owner.execute("DROP FUNCTION " + function)
        elif mutation == "body":
            owner.execute("CREATE OR REPLACE FUNCTION skeleton.assert_user_deactivation(target_user uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,skeleton AS $$ BEGIN RETURN; END $$")
        elif mutation == "search_path":
            owner.execute("ALTER FUNCTION " + function + " SET search_path=public,skeleton")
        elif mutation == "security":
            owner.execute("ALTER FUNCTION " + function + " SECURITY INVOKER")
        elif mutation == "owner":
            owner.execute("GRANT CREATE ON SCHEMA skeleton TO skeleton_audit_writer")
            owner.execute("ALTER FUNCTION " + function + " OWNER TO skeleton_audit_writer")
            owner.execute("REVOKE CREATE ON SCHEMA skeleton FROM skeleton_audit_writer")
        elif mutation == "public_execute":
            owner.execute("GRANT EXECUTE ON FUNCTION " + function + " TO PUBLIC")
        elif mutation == "runtime_execute":
            owner.execute("REVOKE EXECUTE ON FUNCTION " + function + " FROM skeleton_runtime")
        elif mutation == "grant_option":
            owner.execute("GRANT EXECUTE ON FUNCTION " + function + " TO skeleton_runtime WITH GRANT OPTION")
        elif mutation == "signature":
            owner.execute("CREATE FUNCTION skeleton.assert_user_deactivation(target_user text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,skeleton AS $$ BEGIN RETURN; END $$")
            owner.execute("REVOKE ALL ON FUNCTION skeleton.assert_user_deactivation(text) FROM PUBLIC")
        elif mutation == "return_type":
            owner.execute("DROP FUNCTION " + function)
            owner.execute("CREATE FUNCTION skeleton.assert_user_deactivation(target_user uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,skeleton AS $$ BEGIN RETURN true; END $$")
            owner.execute("REVOKE ALL ON FUNCTION " + function + " FROM PUBLIC")
            owner.execute("GRANT EXECUTE ON FUNCTION " + function + " TO skeleton_runtime")
    errors = check_deployed_schema()
    assert any("assert_user_deactivation" in error and "lifecycle" in error for error in errors), errors


@pytest.mark.parametrize("mutation", ["target", "target_order", "not_valid", "delete_action", "deferrable", "match"])
def test_rd04_composite_foreign_key_matches_historical_target(pristine_schema, mutation):
    table = MemberRole._meta.db_table
    constraint = table + "_sk_org_membership_id"
    target = Role._meta.db_table if mutation == "target" else Membership._meta.db_table
    target_columns = "sysid,organization_id" if mutation == "target_order" else "organization_id,sysid"
    options = {
        "not_valid": "NOT VALID", "delete_action": "ON DELETE CASCADE",
        "deferrable": "DEFERRABLE INITIALLY DEFERRED", "match": "MATCH FULL",
    }.get(mutation, "")
    with psycopg.connect(pristine_schema.owner, autocommit=True) as owner:
        owner.execute(sql.SQL("ALTER TABLE skeleton.{} DROP CONSTRAINT {}").format(sql.Identifier(table), sql.Identifier(constraint)))
        owner.execute(sql.SQL("ALTER TABLE skeleton.{} ADD CONSTRAINT {} FOREIGN KEY (organization_id,membership_id) REFERENCES skeleton.{} ({}) {}").format(
            sql.Identifier(table), sql.Identifier(constraint), sql.Identifier(target),
            sql.SQL(target_columns), sql.SQL(options),
        ))
    errors = check_deployed_schema()
    assert any(table + ".membership_id" in error and "composite tenant foreign key" in error for error in errors), errors


def test_rd04_lifecycle_procedure_cannot_replace_required_function(pristine_schema):
    with psycopg.connect(pristine_schema.owner, autocommit=True) as owner:
        body = owner.execute("SELECT prosrc FROM pg_proc WHERE oid='skeleton.assert_user_deactivation(uuid)'::regprocedure").fetchone()[0]
        owner.execute("DROP FUNCTION skeleton.assert_user_deactivation(uuid)")
        owner.execute(sql.SQL("CREATE PROCEDURE skeleton.assert_user_deactivation(target_user uuid) LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,skeleton AS {}").format(sql.Literal(body)))
        owner.execute("REVOKE ALL ON PROCEDURE skeleton.assert_user_deactivation(uuid) FROM PUBLIC")
        owner.execute("GRANT EXECUTE ON PROCEDURE skeleton.assert_user_deactivation(uuid) TO skeleton_runtime")
    errors = check_deployed_schema()
    assert any("assert_user_deactivation" in error and "lifecycle" in error for error in errors), errors
