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


# One database per pytest-xdist worker: derived names keep every barrier of the base one.


def test_worker_database_keeps_the_disposable_suffix_and_the_rest_of_the_url():
    assert idb.worker_database_name("ci_skeleton_test", "gw0") == "ci_gw0_skeleton_test"
    assert idb.worker_database_name("local_skeleton_test", "gw15") == "local_gw15_skeleton_test"
    url = idb.worker_database_url(OWNER + "?sslmode=disable", "gw3")
    assert url == "postgres://skeleton_owner:owner-secret@127.0.0.1:5432/ci_gw3_skeleton_test?sslmode=disable"


@pytest.mark.parametrize("worker", ["", "master", "gw", "gw-1", "gw01", "gw1; DROP DATABASE x", "GW1", "gw1000", None])
def test_worker_database_rejects_anything_but_an_xdist_worker_id(worker):
    with pytest.raises(RuntimeError, match="parallel worker id"):
        idb.worker_database_name("ci_skeleton_test", worker)


@pytest.mark.parametrize("database", ["production", "ci_skeleton_test_copy", "_skeleton_test", ""])
def test_worker_database_is_derived_only_from_a_disposable_name(database):
    with pytest.raises(RuntimeError, match="_skeleton_test"):
        idb.worker_database_name(database, "gw0")


@pytest.mark.parametrize(
    "url", ["dbname=ci_skeleton_test user=skeleton_owner", "sqlite:///ci_skeleton_test", OWNER[:-17]]
)
def test_worker_database_url_requires_a_postgres_url_with_a_database(url):
    with pytest.raises(RuntimeError):
        idb.worker_database_url(url, "gw0")


def test_select_worker_database_rewrites_both_variables_once():
    environment = {idb.RUNTIME_ENV: RUNTIME, idb.OWNER_ENV: OWNER, idb.WORKER_ENV: "gw2"}
    assert idb.select_worker_database(environment) == "ci_gw2_skeleton_test"
    assert environment[idb.RUNTIME_ENV].endswith("@127.0.0.1:5432/ci_gw2_skeleton_test")
    assert environment[idb.OWNER_ENV].endswith("@127.0.0.1:5432/ci_gw2_skeleton_test")
    assert "skeleton_runtime:runtime-secret" in environment[idb.RUNTIME_ENV]
    assert "skeleton_owner:owner-secret" in environment[idb.OWNER_ENV]
    with pytest.raises(RuntimeError, match="already selected"):
        idb.select_worker_database(environment)
    assert environment[idb.RUNTIME_ENV].endswith("/ci_gw2_skeleton_test")


def test_select_worker_database_does_nothing_outside_a_worker_or_without_urls():
    serial = {idb.RUNTIME_ENV: RUNTIME, idb.OWNER_ENV: OWNER}
    assert idb.select_worker_database(serial) is None
    assert serial == {idb.RUNTIME_ENV: RUNTIME, idb.OWNER_ENV: OWNER}
    unit_only = {idb.WORKER_ENV: "gw0"}
    assert idb.select_worker_database(unit_only) is None
    assert unit_only == {idb.WORKER_ENV: "gw0"}


def test_pair_refuses_the_shared_database_inside_a_worker(monkeypatch):
    """A worker that skipped the selection would recreate the schema under the other workers."""
    monkeypatch.setenv("SKELETON_TEST_ALLOW_SCHEMA_RESET", "1")
    monkeypatch.setenv(idb.RUNTIME_ENV, RUNTIME)
    monkeypatch.setenv(idb.OWNER_ENV, OWNER)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv(idb.WORKER_ENV, "gw1")
    monkeypatch.delenv(idb._SELECTED_ENV, raising=False)
    with pytest.raises(RuntimeError, match="select its own database"):
        idb.disposable_database_pair()
    idb.select_worker_database()
    pair = idb.disposable_database_pair()
    assert pair.database == "ci_gw1_skeleton_test"
    assert pair.runtime.endswith("/ci_gw1_skeleton_test") and pair.owner.endswith("/ci_gw1_skeleton_test")
    monkeypatch.delenv(idb.WORKER_ENV)
    monkeypatch.delenv(idb._SELECTED_ENV)
    monkeypatch.setenv(idb.RUNTIME_ENV, RUNTIME)
    monkeypatch.setenv(idb.OWNER_ENV, OWNER)
    assert idb.disposable_database_pair().database == "ci_skeleton_test"


def test_worker_database_urls_list_the_first_workers_in_order():
    urls = idb.worker_database_urls(3, {idb.RUNTIME_ENV: RUNTIME, idb.OWNER_ENV: OWNER})
    assert [owner.rsplit("/", 1)[1] for _, owner in urls] == [
        "ci_gw0_skeleton_test",
        "ci_gw1_skeleton_test",
        "ci_gw2_skeleton_test",
    ]
    assert all("skeleton_runtime" in runtime and "skeleton_owner" in owner for runtime, owner in urls)
    with pytest.raises(RuntimeError, match="both required"):
        idb.worker_database_urls(1, {idb.RUNTIME_ENV: RUNTIME})


def test_missing_worker_databases_only_connects_and_reports_names(monkeypatch):
    import psycopg

    opened = []

    class Connection:
        def close(self):
            opened.append("closed")

    def connect(dsn, **kwargs):
        opened.append(dsn.rsplit("/", 1)[1])
        if dsn.endswith("/ci_gw1_skeleton_test"):
            raise psycopg.OperationalError('database "ci_gw1_skeleton_test" does not exist')
        assert "skeleton_owner" in dsn, "The probe uses the owner URL, never a superuser."
        return Connection()

    monkeypatch.setattr(psycopg, "connect", connect)
    environment = {idb.RUNTIME_ENV: RUNTIME, idb.OWNER_ENV: OWNER}
    assert idb.missing_worker_databases(3, environment) == ["ci_gw1_skeleton_test"]
    assert opened == ["ci_gw0_skeleton_test", "closed", "ci_gw1_skeleton_test", "ci_gw2_skeleton_test", "closed"]
