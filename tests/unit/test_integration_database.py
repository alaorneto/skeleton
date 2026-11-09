"""Schema template helpers refuse anything but the validated disposable owner, without a database."""

import pytest

from tools import integration_database as idb

OWNER = "postgres://skeleton_owner:owner-secret@127.0.0.1:5432/ci_skeleton_test"
RUNTIME = "postgres://skeleton_runtime:runtime-secret@127.0.0.1:5432/ci_skeleton_test"
PAIR = idb.DatabasePair(RUNTIME, OWNER, "ci_skeleton_test")


class FakeConnection:
    def __init__(self, identity=("ci_skeleton_test", "skeleton_owner"), role=(False, False, False)):
        self.rows = [identity, role]
        self.executed = []
        self.closed = False

    def execute(self, query, *args):
        self.executed.append(query)
        outer = self

        class Result:
            def fetchone(self):
                return outer.rows.pop(0) if outer.rows else None

        return Result()

    def close(self):
        self.closed = True


@pytest.fixture
def fake_connect(monkeypatch):
    import psycopg

    made = []

    def install(**kwargs):
        def connect(dsn, **_):
            assert dsn == OWNER, "Schema helpers connect only with the validated owner DSN."
            made.append(FakeConnection(**kwargs))
            return made[-1]

        monkeypatch.setattr(psycopg, "connect", connect)
        return made

    return install


def test_verify_owner_accepts_unprivileged_owner_of_validated_database():
    idb._verify_owner(FakeConnection(), PAIR)


@pytest.mark.parametrize(
    "identity",
    [("ci_skeleton_test", "postgres"), ("production", "skeleton_owner"), ("ci_skeleton_test", "skeleton_runtime")],
)
def test_verify_owner_rejects_connected_identity_divergent_from_pair(identity):
    with pytest.raises(RuntimeError, match="identity differs"):
        idb._verify_owner(FakeConnection(identity=identity), PAIR)


@pytest.mark.parametrize("role", [(True, False, False), (False, True, False), (False, False, True), None])
def test_verify_owner_rejects_superuser_createdb_createrole_or_unknown_role(role):
    with pytest.raises(RuntimeError, match="must not be a PostgreSQL superuser"):
        idb._verify_owner(FakeConnection(role=role), PAIR)


@pytest.mark.parametrize("helper", [idb.reset_schema, idb.schema_fingerprint])
def test_helpers_reject_pairs_not_built_by_disposable_database_pair(helper):
    forged = ("postgres://skeleton_owner@127.0.0.1/prod", OWNER, "prod")
    with pytest.raises(RuntimeError, match="disposable_database_pair"):
        helper(forged)


def test_restore_rejects_foreign_template_and_forged_pair():
    with pytest.raises(RuntimeError, match="SchemaTemplate"):
        idb.restore_schema(PAIR, "DROP SCHEMA skeleton CASCADE;")
    with pytest.raises(RuntimeError, match="disposable_database_pair"):
        idb.restore_schema(("x", OWNER, "ci_skeleton_test"), idb.SchemaTemplate(sql="", fingerprint={}))


def test_restore_refuses_privileged_connection_before_any_ddl(fake_connect):
    made = fake_connect(role=(True, False, False))
    with pytest.raises(RuntimeError, match="superuser"):
        idb.restore_schema(PAIR, idb.SchemaTemplate(sql="SELECT 1;", fingerprint={}))
    assert made[0].closed
    assert not any("DROP" in query or "SELECT 1" in query for query in made[0].executed)


def test_dump_refuses_divergent_identity_before_running_pg_dump(fake_connect, monkeypatch):
    fake_connect(identity=("other_db", "skeleton_owner"))
    monkeypatch.setattr(idb.shutil, "which", lambda name: "/usr/bin/pg_dump")

    def forbidden(*args, **kwargs):
        raise AssertionError("pg_dump must not run for an unverified identity.")

    monkeypatch.setattr(idb.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="identity differs"):
        idb.dump_schema(PAIR)


def test_missing_pg_dump_names_both_ways_out(monkeypatch):
    monkeypatch.setattr(idb.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError) as error:
        idb.dump_schema(PAIR)
    assert idb.PG_DUMP_ENV in str(error.value)
    assert f"{idb.TEMPLATE_DISABLE_ENV}=0" in str(error.value)


def test_pg_dump_password_travels_in_environment_not_argv(monkeypatch):
    monkeypatch.setattr(idb.shutil, "which", lambda name: "/usr/bin/pg_dump")
    monkeypatch.setenv("PGPASSWORD", "stale")
    argv, env = idb._pg_dump_command(PAIR)
    assert not any("owner-secret" in part for part in argv)
    assert env["PGPASSWORD"] == "owner-secret"
    assert "--schema=skeleton" in argv
    assert "--no-password" in argv
    dsn = argv[argv.index("--dbname") + 1]
    assert "user=skeleton_owner" in dsn and "dbname=ci_skeleton_test" in dsn


def test_template_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.delenv(idb.TEMPLATE_DISABLE_ENV, raising=False)
    assert idb.template_enabled()
    monkeypatch.setenv(idb.TEMPLATE_DISABLE_ENV, "0")
    assert not idb.template_enabled()


DUMP = """\\restrict AbC123
SET statement_timeout = 0;
CREATE SCHEMA skeleton;
ALTER SCHEMA skeleton OWNER TO skeleton_owner;
CREATE TABLE skeleton.t (id integer);
\\unrestrict AbC123
"""


def test_prepare_dump_drops_restrict_lines_and_create_schema():
    prepared = idb.prepare_dump(DUMP)
    assert "restrict" not in prepared
    assert "CREATE SCHEMA skeleton;" not in prepared
    assert "ALTER SCHEMA skeleton OWNER TO skeleton_owner;" in prepared
    assert "CREATE TABLE skeleton.t (id integer);" in prepared


@pytest.mark.parametrize(
    "dump",
    [
        DUMP.replace("CREATE SCHEMA skeleton;\n", ""),
        DUMP.replace("CREATE SCHEMA skeleton;\n", "CREATE SCHEMA skeleton;\nCREATE SCHEMA skeleton;\n"),
    ],
)
def test_prepare_dump_requires_exactly_one_create_schema(dump):
    with pytest.raises(RuntimeError, match="exactly one CREATE SCHEMA"):
        idb.prepare_dump(dump)


def test_prepare_dump_rejects_other_psql_meta_commands():
    with pytest.raises(RuntimeError, match="meta-command"):
        idb.prepare_dump(DUMP + "\\connect production\n")
