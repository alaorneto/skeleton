"""A schema reused between tests is the schema of the template, or it is recreated (#760).

The suite keeps schema skeleton between two tests and deletes only the rows the first one wrote.
That is only sound if nothing of the first test reaches the second: these tests write rows the way
a test does, change the structure the way the few DDL tests do, and compare the reused schema with
the migrated one by the same fingerprint that proves the template (test_schema_template.py).
"""

import json

import psycopg
import pytest
from django.db import connection
from psycopg import sql

from skeleton.access.models import Role
from skeleton.tenancy.models import Organization
from tools import integration_database as idb

# The fingerprint includes rows: the test precondition must not open apps before comparing (#22).
pytestmark = pytest.mark.closed_app_defaults
PROBE_APP = "fixture_reuse_probe"


@pytest.fixture
def reuse(skeleton_db, postgres_template):
    """The pair, the template and the reuse state the fixture left for this worker database."""
    if postgres_template is None:
        pytest.skip("SKELETON_TEST_POSTGRES_TEMPLATE=0: each test migrates, there is no template.")
    if not idb.schema_reuse_enabled():
        pytest.skip(f"{idb.REUSE_ENV}=0: every test recreates the schema.")
    assert connection.vendor == "postgresql", "The schema reuse exists only for real PostgreSQL."
    pair = idb.disposable_database_pair()
    state = idb.current_schema_reuse(pair)
    assert state is not None, "The migrated schema must be reusable: a real restore for every test is the slow path."
    return pair, postgres_template, state


def _differences(actual, expected):
    return {
        section: sorted(set(map(repr, actual.get(section) or [])) ^ set(map(repr, rows)))[:4]
        for section, rows in expected.items()
        if actual.get(section) != rows
    }


def _write_like_a_test():
    """Committed rows by the runtime role, through the services: tenant tables, audit, permissions."""
    from skeleton.accounts.services import create_identity, create_organization
    from skeleton.tenancy.context import system_context

    with system_context(reason="fixture:schema-reuse"):
        owner = create_identity(email="owner@reuse.example.test", password="Fixture-only-password-2026!")
    organization = create_organization(owner=owner, slug="schema-reuse", name="Schema reuse")
    connection.close()
    return organization


def _probe(owner):
    """A row more in a table the migrations seed; its sequence advances."""
    return owner.execute(
        "INSERT INTO skeleton.django_migrations (app, name, applied) VALUES (%s, '0001_probe', now()) RETURNING id",
        (PROBE_APP,),
    ).fetchone()[0]


def _rows_without_rls(owner, table):
    """How many rows a table really holds: FORCE ROW LEVEL SECURITY lifted in a rolled-back transaction."""
    with owner.transaction(force_rollback=True):
        owner.execute(sql.SQL("ALTER TABLE skeleton.{} NO FORCE ROW LEVEL SECURITY").format(sql.Identifier(table)))
        return owner.execute(sql.SQL("SELECT count(*) FROM skeleton.{}").format(sql.Identifier(table))).fetchone()[0]


def _template_rows(state, table):
    """How many rows of a table the migrations seeded."""
    return state.seeded[table][2][0] if table in state.seeded else 0


def test_rows_written_by_a_test_are_gone_and_the_schema_is_the_template(reuse):
    pair, template, state = reuse
    _write_like_a_test()
    role_table = Role._meta.db_table
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        first_probe = _probe(owner)
        # Positive control: the rows exist, behind row level security the owner does not bypass.
        assert _rows_without_rls(owner, Organization._meta.db_table) == 1
        assert _rows_without_rls(owner, role_table) > 0
        written = idb.rows_outside_template(state)
        assert {Organization._meta.db_table, role_table, "django_migrations", "sk_audit_auditevent"} <= set(written)

        assert idb.reuse_schema(state, template, verify=True), state.reason

        assert _differences(idb.schema_fingerprint(pair), template.fingerprint) == {}
        assert idb.rows_outside_template(state) == []
        for table in written:
            assert _rows_without_rls(owner, table) == _template_rows(state, table), table
        # Sequences are back too: the same insert gets the same key in the next test.
        assert _probe(owner) == first_probe


def test_a_reused_schema_still_enforces_row_level_security_and_audits(reuse):
    pair, template, state = reuse
    _write_like_a_test()
    assert idb.reuse_schema(state, template), state.reason
    # The flags and triggers lifted to delete the rows are back: the runtime role is audited
    # and sees no row of an organization without its context.
    organization = _write_like_a_test()
    assert not Role.all_objects.filter(organization_id=organization.pk).exists()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        assert _rows_without_rls(owner, Organization._meta.db_table) == 1
        assert _rows_without_rls(owner, Role._meta.db_table) > 0
        assert _rows_without_rls(owner, "sk_audit_auditevent") > _template_rows(state, "sk_audit_auditevent")
        forced, enabled = owner.execute(
            "SELECT (SELECT count(*) FROM pg_class WHERE relnamespace='skeleton'::regnamespace AND relforcerowsecurity),"
            " (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid"
            "  WHERE c.relnamespace='skeleton'::regnamespace AND NOT t.tgisinternal AND t.tgenabled <> 'O')"
        ).fetchone()
    assert forced == len(state.forced) > 0
    assert enabled == 0


def _first(owner, query):
    row = owner.execute(query).fetchone()
    assert row, query
    return row


def _role_statement(statement):
    return lambda owner: owner.execute(sql.SQL(statement).format(sql.Identifier(Role._meta.db_table)))


def _drop_first_policy(owner):
    policy, table = _first(
        owner, "SELECT policyname, tablename FROM pg_policies WHERE schemaname='skeleton' ORDER BY 2, 1 LIMIT 1"
    )
    owner.execute(sql.SQL("DROP POLICY {} ON skeleton.{}").format(sql.Identifier(policy), sql.Identifier(table)))


def _grant_first_sequence(owner):
    sequence = _first(
        owner,
        "SELECT relname FROM pg_class WHERE relnamespace='skeleton'::regnamespace AND relkind='S' ORDER BY 1 LIMIT 1",
    )[0]
    owner.execute(sql.SQL("GRANT USAGE ON SEQUENCE skeleton.{} TO skeleton_runtime").format(sql.Identifier(sequence)))


def _alter_first_function(owner):
    owner.execute(
        "DO $$ DECLARE target regprocedure; BEGIN"
        " SELECT p.oid::regprocedure INTO STRICT target FROM pg_proc p"
        "  WHERE p.pronamespace='skeleton'::regnamespace AND p.prokind='f' ORDER BY p.proname, p.oid LIMIT 1;"
        " EXECUTE format('ALTER FUNCTION %s COST 7', target); END $$"
    )


# In this order on one schema: each statement must change the marker the statement before left.
STRUCTURE_CHANGES = (
    ("policy dropped", _drop_first_policy),
    ("policy added", _role_statement("CREATE POLICY fixture_reuse ON skeleton.{} TO skeleton_runtime USING (true)")),
    ("triggers disabled", _role_statement("ALTER TABLE skeleton.{} DISABLE TRIGGER USER")),
    ("triggers enabled again", _role_statement("ALTER TABLE skeleton.{} ENABLE TRIGGER USER")),
    (
        "trigger dropped",
        lambda owner: owner.execute(
            sql.SQL("DROP TRIGGER {} ON skeleton.{}").format(
                sql.Identifier(Role._meta.db_table + "_sk_audit"), sql.Identifier(Role._meta.db_table)
            )
        ),
    ),
    ("table grant revoked", _role_statement("REVOKE SELECT ON skeleton.{} FROM skeleton_runtime")),
    ("sequence grant added", _grant_first_sequence),
    ("schema grant added", lambda owner: owner.execute("GRANT CREATE ON SCHEMA skeleton TO skeleton_runtime")),
    ("force row level security lifted", _role_statement("ALTER TABLE skeleton.{} NO FORCE ROW LEVEL SECURITY")),
    ("force row level security put back", _role_statement("ALTER TABLE skeleton.{} FORCE ROW LEVEL SECURITY")),
    ("row level security disabled", _role_statement("ALTER TABLE skeleton.{} DISABLE ROW LEVEL SECURITY")),
    ("table created", lambda owner: owner.execute("CREATE TABLE skeleton.fixture_reuse_t (id integer PRIMARY KEY)")),
    ("view created", lambda owner: owner.execute("CREATE VIEW skeleton.fixture_reuse_v AS SELECT 1 AS one")),
    ("index created", _role_statement("CREATE INDEX fixture_reuse_i ON skeleton.{} (name)")),
    ("column added", _role_statement("ALTER TABLE skeleton.{} ADD COLUMN fixture_reuse integer")),
    ("column dropped", _role_statement("ALTER TABLE skeleton.{} DROP COLUMN fixture_reuse")),
    ("column default changed", _role_statement("ALTER TABLE skeleton.{} ALTER COLUMN name SET DEFAULT 'fixture'")),
    ("column made nullable", _role_statement("ALTER TABLE skeleton.{} ALTER COLUMN description DROP NOT NULL")),
    ("constraint added", _role_statement("ALTER TABLE skeleton.{} ADD CONSTRAINT fixture_reuse CHECK (name <> '')")),
    (
        "function created",
        lambda owner: owner.execute(
            "CREATE FUNCTION skeleton.fixture_reuse() RETURNS integer LANGUAGE sql AS 'SELECT 1'"
        ),
    ),
    ("function altered", _alter_first_function),
    ("type created", lambda owner: owner.execute("CREATE TYPE skeleton.fixture_reuse_e AS ENUM ('one')")),
    ("enum value added", lambda owner: owner.execute("ALTER TYPE skeleton.fixture_reuse_e ADD VALUE 'two'")),
    ("sequence created", lambda owner: owner.execute("CREATE SEQUENCE skeleton.fixture_reuse_s")),
    (
        "operator created",
        lambda owner: owner.execute(
            "CREATE OPERATOR skeleton.=== (FUNCTION = int4eq, LEFTARG = integer, RIGHTARG = integer)"
        ),
    ),
    ("table truncated", _role_statement("TRUNCATE skeleton.{} CASCADE")),
)


def test_every_change_of_structure_changes_the_catalog_marker(reuse, subtests):
    pair, template, state = reuse
    _write_like_a_test()
    with psycopg.connect(pair.owner, autocommit=True) as owner:

        def marker():
            return owner.execute(idb._REUSE_STABLE).fetchall(), owner.execute(idb._REUSE_VOLATILE).fetchall()

        assert marker() == (state.stable, state.volatile), "Rows written by a test are not a change of structure."
        for case, change in STRUCTURE_CHANGES:
            before = marker()
            change(owner)
            with subtests.test(case=case):
                assert marker() != before
                assert not idb.reuse_schema(state, template)
                assert "changed" in state.reason


def test_a_table_created_and_dropped_leaves_the_schema_at_the_template(reuse):
    pair, template, state = reuse
    _write_like_a_test()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        owner.execute(
            "CREATE TABLE skeleton.fixture_reuse_t (id integer PRIMARY KEY); DROP TABLE skeleton.fixture_reuse_t"
        )

    # Nothing of it is left in the catalog, and the whole comparison agrees.
    assert idb.reuse_schema(state, template, verify=True), state.reason


def _recreate(owner, pair, template):
    idb.restore_schema(pair, template)


REFUSED = {
    "policy_dropped": lambda owner, pair, template: _drop_first_policy(owner),
    "force_rls_lifted": lambda owner, pair, template: _role_statement(
        "ALTER TABLE skeleton.{} NO FORCE ROW LEVEL SECURITY"
    )(owner),
    "schema_recreated": _recreate,
}


@pytest.mark.parametrize("change", sorted(REFUSED))
def test_a_change_the_deletes_cannot_undo_recreates_the_schema(reuse, change):
    pair, template, state = reuse
    _write_like_a_test()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        REFUSED[change](owner, pair, template)
        if change != "schema_recreated":
            assert idb.schema_fingerprint(pair) != template.fingerprint, "The change must be one the fingerprint sees."

        assert not idb.reuse_schema(state, template), "The reuse must refuse a schema it cannot prove equal."
        assert "changed" in state.reason
        if change != "schema_recreated":
            # Refused as a whole: the rows of the test are still there, nothing was half cleaned.
            assert _rows_without_rls(owner, Organization._meta.db_table) == 1

    assert idb.template_schema(pair, template) == "restore"
    assert _differences(idb.schema_fingerprint(pair), template.fingerprint) == {}
    fresh = idb.current_schema_reuse(pair)
    assert fresh is not None and fresh is not state
    assert idb.rows_outside_template(fresh) == []
    # And the recreated schema is reusable again.
    assert idb.template_schema(pair, template) == "reuse"


LAST_MIGRATION = "id = (SELECT max(id) FROM skeleton.django_migrations)"
SEEDED_CHANGES = {
    "row_deleted": f"DELETE FROM skeleton.django_migrations WHERE {LAST_MIGRATION}",
    "row_changed": f"UPDATE skeleton.django_migrations SET name = name || '_changed' WHERE {LAST_MIGRATION}",
    "every_row_changed": "UPDATE skeleton.django_migrations SET applied = applied + interval '1 day'",
    "rows_of_two_tables": (
        f"DELETE FROM skeleton.django_migrations WHERE {LAST_MIGRATION};"
        " UPDATE skeleton.auth_permission SET name = name || ' (changed)'"
    ),
}


@pytest.mark.parametrize("change", sorted(SEEDED_CHANGES))
def test_seeded_rows_a_test_changed_or_deleted_are_put_back(reuse, change):
    pair, template, state = reuse
    _write_like_a_test()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        owner.execute(SEEDED_CHANGES[change])
        _probe(owner)
    assert idb.schema_fingerprint(pair) != template.fingerprint, "The change must be one the fingerprint sees."

    assert idb.reuse_schema(state, template, verify=True), state.reason

    # The fingerprint compares every row of the seeded tables with the rows the migrations wrote.
    assert _differences(idb.schema_fingerprint(pair), template.fingerprint) == {}
    assert idb.rows_outside_template(state) == []


def test_seeded_rows_that_cannot_be_put_back_exactly_recreate_the_schema(reuse):
    pair, template, state = reuse
    _write_like_a_test()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        last = owner.execute("SELECT max(id) FROM skeleton.django_migrations").fetchone()[0]
        owner.execute("DELETE FROM skeleton.django_migrations WHERE id = %s", (last,))
        # The copy of the template rows is the only source of a seeded row: an incomplete or
        # inexact copy must never pass as the template.
        columns, rows = state.seed_rows["django_migrations"]
        state.seed_rows["django_migrations"] = (
            columns,
            json.dumps([row for row in json.loads(rows) if row["id"] != last]),
        )

        assert not idb.reuse_schema(state, template)
        assert "could not be put back (django_migrations)" in state.reason
        # Refused as a whole: the rows of the test are still there, nothing was half cleaned.
        assert _rows_without_rls(owner, Organization._meta.db_table) == 1

    assert idb.template_schema(pair, template) == "restore"
    assert _differences(idb.schema_fingerprint(pair), template.fingerprint) == {}


def test_a_lock_of_another_connection_refuses_the_reuse_and_leaves_nothing_half_clean(reuse):
    pair, template, state = reuse
    _write_like_a_test()
    table = Organization._meta.db_table
    state.lock_timeout = "300ms"
    with psycopg.connect(pair.owner, autocommit=True) as owner, psycopg.connect(pair.owner) as writer:
        # What a connection a test left open in a transaction holds on the tables it wrote.
        writer.execute(sql.SQL("LOCK TABLE skeleton.{} IN ROW EXCLUSIVE MODE").format(sql.Identifier(table)))

        assert not idb.reuse_schema(state, template)
        assert "LockNotAvailable" in state.reason

        # The transaction of the reuse rolled back as a whole: flags, triggers and rows as they were.
        assert owner.execute(idb._REUSE_FLAGS).fetchall() == state.flags
        assert owner.execute(idb._REUSE_VOLATILE).fetchall() == state.volatile
        writer.rollback()
        assert _rows_without_rls(owner, table) == 1
        # Nothing changed in the structure: with the lock gone the same state is good again.
        assert idb.reuse_schema(state, template, verify=True), state.reason
        assert _rows_without_rls(owner, table) == 0


def test_a_terminated_owner_connection_recreates_the_schema(reuse):
    pair, template, state = reuse
    _write_like_a_test()
    state.connection.close()

    assert not idb.reuse_schema(state, template)
    assert idb.template_schema(pair, template) == "restore"
    assert _differences(idb.schema_fingerprint(pair), template.fingerprint) == {}


def test_the_whole_comparison_fails_loudly_when_the_reuse_leaves_a_row(reuse, monkeypatch):
    pair, template, state = reuse
    _write_like_a_test()
    # A defect of the reuse itself, not a change of the test: it must never pass as a clean schema.
    monkeypatch.setattr(idb, "_delete_written_rows", lambda state, dirty: None)

    with pytest.raises(RuntimeError, match="rows left in"):
        idb.reuse_schema(state, template, verify=True)


def test_reuse_switched_off_recreates_the_schema_for_every_test(reuse, monkeypatch):
    pair, template, state = reuse
    monkeypatch.setenv(idb.REUSE_ENV, "0")

    assert idb.template_schema(pair, template) == "restore"
    assert idb.current_schema_reuse(pair) is None
    assert idb.template_schema(pair, template) == "restore"
    assert _differences(idb.schema_fingerprint(pair), template.fingerprint) == {}


def test_every_nth_reuse_is_compared_with_the_whole_fingerprint(reuse, monkeypatch):
    pair, template, state = reuse
    calls = []
    original = idb._fingerprint
    monkeypatch.setattr(idb, "_fingerprint", lambda connection: calls.append(1) or original(connection))
    monkeypatch.setenv(idb.REUSE_VERIFY_ENV, "2")
    state.reused = 0

    assert [idb.template_schema(pair, template) for _ in range(4)] == ["reuse"] * 4
    assert len(calls) == 2
