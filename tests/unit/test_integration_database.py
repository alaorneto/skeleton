"""Schema template helpers refuse anything but the validated disposable owner, without a database.

The worker databases and the template live in the distributed kit (#573), ``kit`` here; the core
suite re-exports them from ``tools/integration_database.py`` (``idb``), which keeps only the cache.
"""

import pytest

from skeleton.testing import database as kit
from tools import integration_database as idb

OWNER = "postgres://skeleton_owner:owner-secret@127.0.0.1:5432/ci_skeleton_test"
RUNTIME = "postgres://skeleton_runtime:runtime-secret@127.0.0.1:5432/ci_skeleton_test"
PAIR = kit.DatabasePair(RUNTIME, OWNER, "ci_skeleton_test")


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
    kit._verify_owner(FakeConnection(), PAIR)


@pytest.mark.parametrize(
    "identity",
    [("ci_skeleton_test", "postgres"), ("production", "skeleton_owner"), ("ci_skeleton_test", "skeleton_runtime")],
)
def test_verify_owner_rejects_connected_identity_divergent_from_pair(identity):
    with pytest.raises(RuntimeError, match="identity differs"):
        kit._verify_owner(FakeConnection(identity=identity), PAIR)


@pytest.mark.parametrize("role", [(True, False, False), (False, True, False), (False, False, True), None])
def test_verify_owner_rejects_superuser_createdb_createrole_or_unknown_role(role):
    with pytest.raises(RuntimeError, match="must not be a PostgreSQL superuser"):
        kit._verify_owner(FakeConnection(role=role), PAIR)


@pytest.mark.parametrize("helper", [kit.reset_schema, kit.schema_fingerprint])
def test_helpers_reject_pairs_not_built_by_disposable_database_pair(helper):
    forged = ("postgres://skeleton_owner@127.0.0.1/prod", OWNER, "prod")
    with pytest.raises(RuntimeError, match="disposable_database_pair"):
        helper(forged)


def test_restore_rejects_foreign_template_and_forged_pair():
    with pytest.raises(RuntimeError, match="SchemaTemplate"):
        kit.restore_schema(PAIR, "DROP SCHEMA skeleton CASCADE;")
    with pytest.raises(RuntimeError, match="disposable_database_pair"):
        kit.restore_schema(("x", OWNER, "ci_skeleton_test"), kit.SchemaTemplate(sql="", fingerprint={}))


def test_restore_refuses_privileged_connection_before_any_ddl(fake_connect):
    made = fake_connect(role=(True, False, False))
    with pytest.raises(RuntimeError, match="superuser"):
        kit.restore_schema(PAIR, kit.SchemaTemplate(sql="SELECT 1;", fingerprint={}))
    assert made[0].closed
    assert not any("DROP" in query or "SELECT 1" in query for query in made[0].executed)


def test_dump_refuses_divergent_identity_before_running_pg_dump(fake_connect, monkeypatch):
    fake_connect(identity=("other_db", "skeleton_owner"))
    monkeypatch.setattr(kit.shutil, "which", lambda name: "/usr/bin/pg_dump")

    def forbidden(*args, **kwargs):
        raise AssertionError("pg_dump must not run for an unverified identity.")

    monkeypatch.setattr(kit.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="identity differs"):
        kit.dump_schema(PAIR)


def test_missing_pg_dump_names_both_ways_out(monkeypatch):
    monkeypatch.setattr(kit.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError) as error:
        kit.dump_schema(PAIR)
    assert kit.PG_DUMP_ENV in str(error.value)
    assert f"{kit.TEMPLATE_DISABLE_ENV}=0" in str(error.value)


def test_pg_dump_password_travels_in_environment_not_argv(monkeypatch):
    monkeypatch.setattr(kit.shutil, "which", lambda name: "/usr/bin/pg_dump")
    monkeypatch.setenv("PGPASSWORD", "stale")
    argv, env = kit._pg_dump_command(PAIR)
    assert not any("owner-secret" in part for part in argv)
    assert env["PGPASSWORD"] == "owner-secret"
    assert "--schema=skeleton" in argv
    assert "--no-password" in argv
    dsn = argv[argv.index("--dbname") + 1]
    assert "user=skeleton_owner" in dsn and "dbname=ci_skeleton_test" in dsn


def test_template_can_be_disabled_explicitly(monkeypatch):
    monkeypatch.delenv(kit.TEMPLATE_DISABLE_ENV, raising=False)
    assert kit.template_enabled()
    monkeypatch.setenv(kit.TEMPLATE_DISABLE_ENV, "0")
    assert not kit.template_enabled()


DUMP = """\\restrict AbC123
SET statement_timeout = 0;
CREATE SCHEMA skeleton;
ALTER SCHEMA skeleton OWNER TO skeleton_owner;
CREATE TABLE skeleton.t (id integer);
\\unrestrict AbC123
"""


def test_prepare_dump_drops_restrict_lines_and_create_schema():
    prepared = kit.prepare_dump(DUMP)
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
        kit.prepare_dump(dump)


def test_prepare_dump_rejects_other_psql_meta_commands():
    with pytest.raises(RuntimeError, match="meta-command"):
        kit.prepare_dump(DUMP + "\\connect production\n")


# One database per pytest-xdist worker: derived names keep every barrier of the base one.


def test_worker_database_keeps_the_disposable_suffix_and_the_rest_of_the_url():
    assert kit.worker_database_name("ci_skeleton_test", "gw0") == "ci_gw0_skeleton_test"
    assert kit.worker_database_name("local_skeleton_test", "gw15") == "local_gw15_skeleton_test"
    url = kit.worker_database_url(OWNER + "?sslmode=disable", "gw3")
    assert url == "postgres://skeleton_owner:owner-secret@127.0.0.1:5432/ci_gw3_skeleton_test?sslmode=disable"


@pytest.mark.parametrize("worker", ["", "master", "gw", "gw-1", "gw01", "gw1; DROP DATABASE x", "GW1", "gw1000", None])
def test_worker_database_rejects_anything_but_an_xdist_worker_id(worker):
    with pytest.raises(RuntimeError, match="parallel worker id"):
        kit.worker_database_name("ci_skeleton_test", worker)


@pytest.mark.parametrize("database", ["production", "ci_skeleton_test_copy", "_skeleton_test", ""])
def test_worker_database_is_derived_only_from_a_disposable_name(database):
    with pytest.raises(RuntimeError, match="_skeleton_test"):
        kit.worker_database_name(database, "gw0")


@pytest.mark.parametrize(
    "url", ["dbname=ci_skeleton_test user=skeleton_owner", "sqlite:///ci_skeleton_test", OWNER[:-17]]
)
def test_worker_database_url_requires_a_postgres_url_with_a_database(url):
    with pytest.raises(RuntimeError):
        kit.worker_database_url(url, "gw0")


def test_select_worker_database_rewrites_both_variables_once():
    environment = {kit.RUNTIME_ENV: RUNTIME, kit.OWNER_ENV: OWNER, kit.WORKER_ENV: "gw2"}
    assert kit.select_worker_database(environment) == "ci_gw2_skeleton_test"
    assert environment[kit.RUNTIME_ENV].endswith("@127.0.0.1:5432/ci_gw2_skeleton_test")
    assert environment[kit.OWNER_ENV].endswith("@127.0.0.1:5432/ci_gw2_skeleton_test")
    assert "skeleton_runtime:runtime-secret" in environment[kit.RUNTIME_ENV]
    assert "skeleton_owner:owner-secret" in environment[kit.OWNER_ENV]
    with pytest.raises(RuntimeError, match="already selected"):
        kit.select_worker_database(environment)
    assert environment[kit.RUNTIME_ENV].endswith("/ci_gw2_skeleton_test")


def test_select_worker_database_does_nothing_outside_a_worker_or_without_urls():
    serial = {kit.RUNTIME_ENV: RUNTIME, kit.OWNER_ENV: OWNER}
    assert kit.select_worker_database(serial) is None
    assert serial == {kit.RUNTIME_ENV: RUNTIME, kit.OWNER_ENV: OWNER}
    unit_only = {kit.WORKER_ENV: "gw0"}
    assert kit.select_worker_database(unit_only) is None
    assert unit_only == {kit.WORKER_ENV: "gw0"}


def test_pair_refuses_the_shared_database_inside_a_worker(monkeypatch):
    """A worker that skipped the selection would recreate the schema under the other workers."""
    monkeypatch.setenv("SKELETON_TEST_ALLOW_SCHEMA_RESET", "1")
    monkeypatch.setenv(kit.RUNTIME_ENV, RUNTIME)
    monkeypatch.setenv(kit.OWNER_ENV, OWNER)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv(kit.WORKER_ENV, "gw1")
    monkeypatch.delenv(kit._SELECTED_ENV, raising=False)
    with pytest.raises(RuntimeError, match="select its own database"):
        kit.disposable_database_pair()
    kit.select_worker_database()
    pair = kit.disposable_database_pair()
    assert pair.database == "ci_gw1_skeleton_test"
    assert pair.runtime.endswith("/ci_gw1_skeleton_test") and pair.owner.endswith("/ci_gw1_skeleton_test")
    monkeypatch.delenv(kit.WORKER_ENV)
    monkeypatch.delenv(kit._SELECTED_ENV)
    monkeypatch.setenv(kit.RUNTIME_ENV, RUNTIME)
    monkeypatch.setenv(kit.OWNER_ENV, OWNER)
    assert kit.disposable_database_pair().database == "ci_skeleton_test"


def test_worker_database_urls_list_the_first_workers_in_order():
    urls = kit.worker_database_urls(3, {kit.RUNTIME_ENV: RUNTIME, kit.OWNER_ENV: OWNER})
    assert [owner.rsplit("/", 1)[1] for _, owner in urls] == [
        "ci_gw0_skeleton_test",
        "ci_gw1_skeleton_test",
        "ci_gw2_skeleton_test",
    ]
    assert all("skeleton_runtime" in runtime and "skeleton_owner" in owner for runtime, owner in urls)
    with pytest.raises(RuntimeError, match="both required"):
        kit.worker_database_urls(1, {kit.RUNTIME_ENV: RUNTIME})


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
    environment = {kit.RUNTIME_ENV: RUNTIME, kit.OWNER_ENV: OWNER}
    assert kit.missing_worker_databases(3, environment) == ["ci_gw1_skeleton_test"]
    assert opened == ["ci_gw0_skeleton_test", "closed", "ci_gw1_skeleton_test", "ci_gw2_skeleton_test", "closed"]


# Template cache between local runs (#350).

TEMPLATE = kit.SchemaTemplate(
    sql="CREATE TABLE skeleton.t (id integer);\n",
    fingerprint={"relations": [("t", "r", "skeleton_owner", True, True, ["skeleton_owner=arwdDxtm/skeleton_owner"])]},
)
KEY_INPUTS = {
    "server_version": "160015",
    "pg_dump_version": "pg_dump (PostgreSQL) 16.15",
    "installed_apps": ["django.contrib.auth", "skeleton.system"],
    "migration_modules": {},
}
SOURCES = {
    "skeleton/system/models.py": "class A: pass\n",
    "skeleton/system/migrations/0001_initial.py": "operations = []\n",
    "skeleton/system/technical_tables.json": "{}\n",
    "config/settings/base.py": "DEBUG = False\n",
    "tests/unit/test_x.py": "def test(): pass\n",
    "tests/conftest.py": "\n",
    "tools/integration_database.py": "\n",
}


@pytest.fixture
def source_tree(tmp_path):
    for name, text in SOURCES.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def _key(root, environment=None, **changes):
    environment = {"DJANGO_SETTINGS_MODULE": "config.settings.test"} if environment is None else environment
    return idb.template_cache_key(**{**KEY_INPUTS, **changes}, environment=environment, root=root)


def test_cache_key_is_stable_and_ignores_the_test_harness(source_tree):
    base = _key(source_tree)
    assert len(base) == 64 and base == _key(source_tree)
    harness = {
        "DJANGO_SETTINGS_MODULE": "config.settings.test",
        "TEST_DATABASE_URL": "postgres://skeleton_runtime:x@h/a_gw1_skeleton_test",
        "SKELETON_TEST_WORKER_DATABASE_SELECTED": "gw1",
        "PYTEST_XDIST_WORKER": "gw1",
    }
    assert _key(source_tree, harness) == base
    (source_tree / "tests/unit/test_x.py").write_text("def test(): assert True\n", encoding="utf-8")
    (source_tree / "skeleton/system/__pycache__").mkdir()
    (source_tree / "skeleton/system/__pycache__/models.cpython-314.pyc").write_bytes(b"\0")
    assert _key(source_tree) == base, "Editing a test or a bytecode cache does not change the schema."


def _edit(path, text):
    def change(root):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")

    return change


@pytest.mark.parametrize(
    "change",
    [
        _edit("skeleton/system/models.py", "class A: x = 1\n"),
        _edit("skeleton/system/migrations/0002_more.py", "\n"),
        _edit("skeleton/system/technical_tables.json", '{"a": 1}\n'),
        _edit("config/settings/base.py", "DEBUG = True\n"),
        _edit("tests/conftest.py", "# changed\n"),
        _edit("tools/integration_database.py", "# changed\n"),
        _edit("applications/tasks/models.py", "\n"),
        _edit("examples/notes/specs.py", "\n"),
        lambda root: (root / "skeleton/system/migrations/0001_initial.py").rename(root / "skeleton/system/x.py"),
    ],
)
def test_cache_key_changes_with_the_code_that_shapes_the_schema(source_tree, change):
    base = _key(source_tree)
    change(source_tree)
    assert _key(source_tree) != base


@pytest.mark.parametrize(
    "changes",
    [
        {"server_version": "170002"},
        {"pg_dump_version": "pg_dump (PostgreSQL) 17.2"},
        {"installed_apps": ["django.contrib.auth", "skeleton.system", "examples.notes"]},
        {"migration_modules": {"sk_system": "archive.sk_system"}},
    ],
)
def test_cache_key_changes_with_server_tools_and_apps(source_tree, changes):
    assert _key(source_tree, **changes) != _key(source_tree)


@pytest.mark.parametrize(
    "environment",
    [
        {"DJANGO_SETTINGS_MODULE": "config.settings.other"},
        {"DJANGO_SETTINGS_MODULE": "config.settings.test", "SKELETON_ENABLE_EXAMPLE_APP": "true"},
        {"DJANGO_SETTINGS_MODULE": "config.settings.test", "SKELETON_BUNDLED_APPS": "calendar"},
    ],
)
def test_cache_key_changes_with_the_project_variables(source_tree, environment):
    assert _key(source_tree, environment) != _key(source_tree)


def test_cache_key_covers_the_installed_library_versions(source_tree, monkeypatch):
    from importlib import metadata

    base = _key(source_tree)

    class Distribution:
        metadata = {"Name": "Django"}
        version = "99.0"

    monkeypatch.setattr(metadata, "distributions", lambda: [Distribution()])
    assert _key(source_tree) != base


def test_cache_directory_can_be_disabled_or_moved(tmp_path):
    assert idb.template_cache_dir({idb.CACHE_ENV: "0"}) is None
    assert idb.template_cache_dir({idb.CACHE_ENV: str(tmp_path)}) == tmp_path
    assert idb.template_cache_dir({}) == idb.ROOT / "var" / "test-template-cache"


def test_cached_template_round_trips_exactly(tmp_path):
    key = "a" * 64
    assert idb.save_cached_template(tmp_path, key, TEMPLATE)
    loaded = idb.load_cached_template(tmp_path, key)
    assert loaded == TEMPLATE and isinstance(loaded.fingerprint["relations"][0], tuple)
    assert not list(tmp_path.glob(".tmp-*")), "The temporary file is renamed, never left behind."
    assert idb.load_cached_template(tmp_path, "b" * 64) is None
    assert idb.load_cached_template(None, key) is None


def _entry(key, **fields):
    import json

    return json.dumps({"format": idb.CACHE_FORMAT, "key": key, "sql": "", "fingerprint": {}, **fields})


@pytest.mark.parametrize(
    "stored",
    [
        lambda key: "not json",
        lambda key: '["a list"]',
        lambda key: _entry(key, format=999),
        lambda key: _entry("b" * 64),
        lambda key: _entry(key, sql=5),
        lambda key: _entry(key, sql="\\connect production\n"),
        lambda key: _entry(key, sql="CREATE SCHEMA skeleton;\n"),
        lambda key: _entry(key, fingerprint={"relations": "x"}),
        lambda key: _entry(key, fingerprint={"relations": ["x"]}),
        lambda key: _entry(key, fingerprint=[]),
    ],
)
def test_malformed_or_foreign_cache_entry_is_ignored(tmp_path, stored):
    key = "a" * 64
    (tmp_path / f"{key}.json").write_text(stored(key), encoding="utf-8")
    assert idb.load_cached_template(tmp_path, key) is None


@pytest.mark.parametrize("key", ["../outside", "A" * 64, "a" * 63, "", None])
def test_cache_refuses_keys_that_are_not_digests(tmp_path, key):
    assert not idb.save_cached_template(tmp_path / "cache", key, TEMPLATE)
    assert idb.load_cached_template(tmp_path / "cache", key) is None
    assert not (tmp_path / "cache").exists()


def test_cache_does_not_store_a_fingerprint_json_cannot_round_trip(tmp_path):
    import datetime

    for fingerprint in ({"data": [("t", datetime.date(2026, 1, 1))]}, {"data": [("t", (1, 2))]}):
        assert not idb.save_cached_template(tmp_path, "a" * 64, kit.SchemaTemplate(sql="", fingerprint=fingerprint))
    assert not list(tmp_path.iterdir())


def test_cache_keeps_only_the_newest_entries(tmp_path):
    import os

    for index in range(idb.CACHE_ENTRIES_KEPT + 3):
        key = f"{index:064x}"
        assert idb.save_cached_template(tmp_path, key, TEMPLATE)
        os.utime(tmp_path / f"{key}.json", (1_000_000 + index, 1_000_000 + index))
    idb.save_cached_template(tmp_path, "f" * 64, TEMPLATE)
    kept = sorted(path.stem for path in tmp_path.glob("*.json"))
    assert len(kept) == idb.CACHE_ENTRIES_KEPT and "f" * 64 in kept
    assert f"{0:064x}" not in kept


def test_cache_removes_the_lock_files_of_entries_it_no_longer_keeps(tmp_path):
    import os

    for index in range(idb.CACHE_ENTRIES_KEPT + 2):
        key = f"{index:064x}"
        (tmp_path / f"{key}.lock").touch()
        assert idb.save_cached_template(tmp_path, key, TEMPLATE)
        os.utime(tmp_path / f"{key}.json", (1_000_000 + index, 1_000_000 + index))
    (tmp_path / f"{'e' * 64}.lock").touch()  # A build that never stored its template.
    newest = "f" * 64
    (tmp_path / f"{newest}.lock").touch()
    assert idb.save_cached_template(tmp_path, newest, TEMPLATE)
    kept = {path.stem for path in tmp_path.glob("*.json")}
    assert {path.stem for path in tmp_path.glob("*.lock")} <= kept and newest in kept
    assert (tmp_path / f"{newest}.lock").exists()


# One build of the template per run (#756): the lock belongs to the process that holds it.

_HOLD_LOCK = """
import sys
from tools import integration_database as idb

with idb.template_build_lock(sys.argv[1], sys.argv[2]) as held:
    print("held" if held else "refused", flush=True)
    sys.stdin.readline()
"""


def _lock_holder(tmp_path, key):
    """Another process holding the build lock until its stdin closes (or it is killed)."""
    import subprocess
    import sys

    process = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(tmp_path), key],
        cwd=idb.ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "held"
    return process


def test_build_lock_is_exclusive_between_processes_and_released_at_exit(tmp_path):
    key = "a" * 64
    holder = _lock_holder(tmp_path, key)
    try:
        with idb.template_build_lock(tmp_path, key, timeout=0) as held:
            assert held is False, "Another process is building this template."
        with idb.template_build_lock(tmp_path, "b" * 64, timeout=0) as held:
            assert held is True, "The lock is per key."
    finally:
        holder.communicate("\n", timeout=30)
    with idb.template_build_lock(tmp_path, key, timeout=30) as held:
        assert held is True


def test_build_lock_of_a_killed_process_does_not_block_the_next_run(tmp_path):
    key = "a" * 64
    holder = _lock_holder(tmp_path, key)
    holder.kill()
    holder.wait(timeout=30)
    holder.stdin.close()
    holder.stdout.close()
    with idb.template_build_lock(tmp_path, key, timeout=30) as held:
        assert held is True


def test_a_waiting_process_gets_the_lock_when_the_builder_finishes(tmp_path):
    import threading

    key = "a" * 64
    holder = _lock_holder(tmp_path, key)
    waiting, outcome = threading.Event(), []

    def wait_for_the_template():
        waiting.set()
        with idb.template_build_lock(tmp_path, key, timeout=30) as held:
            outcome.append(held)

    thread = threading.Thread(target=wait_for_the_template)
    thread.start()
    try:
        assert waiting.wait(timeout=30)
    finally:
        holder.communicate("\n", timeout=30)
    thread.join(timeout=30)
    assert outcome == [True]


@pytest.mark.parametrize("key", ["../outside", "A" * 64, "", None])
def test_build_lock_is_not_taken_without_a_cache_or_a_digest(tmp_path, key):
    with idb.template_build_lock(tmp_path / "cache", key) as held:
        assert held is False
    with idb.template_build_lock(None, "a" * 64) as held:
        assert held is False
    assert not (tmp_path / "cache").exists()


def test_build_lock_fails_open_when_the_directory_cannot_be_used(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    with idb.template_build_lock(blocked, "a" * 64) as held:
        assert held is False, "The caller migrates by itself, as before the lock existed."


# The barriers moved to the wheel for derived projects (#334): same refusals, one implementation.


@pytest.mark.parametrize(
    "change, message",
    [
        ({"SKELETON_TEST_ALLOW_SCHEMA_RESET": None}, "SKELETON_TEST_ALLOW_SCHEMA_RESET=1"),
        (
            {
                "TEST_DATABASE_URL": RUNTIME.replace("ci_skeleton_test", "ci_production"),
                "TEST_MIGRATION_DATABASE_URL": OWNER.replace("ci_skeleton_test", "ci_production"),
            },
            "must end with _skeleton_test",
        ),
        ({"TEST_DATABASE_URL": RUNTIME.replace("skeleton_runtime", "skeleton_owner")}, "skeleton_runtime role"),
        ({"TEST_MIGRATION_DATABASE_URL": OWNER.replace("skeleton_owner", "postgres")}, "skeleton_owner role"),
        ({"DATABASE_URL": RUNTIME}, "must differ from DATABASE_URL"),
    ],
)
def test_disposable_pair_requires_reset_marker_suffix_and_roles(monkeypatch, change, message):
    from skeleton.testing import database

    environment = {
        "SKELETON_TEST_ALLOW_SCHEMA_RESET": "1",
        "TEST_DATABASE_URL": RUNTIME,
        "TEST_MIGRATION_DATABASE_URL": OWNER,
        "DATABASE_URL": None,
        "MIGRATION_DATABASE_URL": None,
        kit.WORKER_ENV: None,
        **change,
    }
    for name, value in environment.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        database.disposable_database_pair()


def test_tools_module_reexports_the_distributed_functions():
    from skeleton.testing import database

    for name in (
        "DatabasePair",
        "disposable_database_pair",
        "reset_schema",
        "_verify_owner",
        "_owner_connection",
        # Worker databases and schema template (#573).
        "SchemaTemplate",
        "dump_schema",
        "restore_schema",
        "schema_fingerprint",
        "template_enabled",
        "select_worker_database",
        "worker_database_name",
        "worker_database_urls",
        "missing_worker_databases",
        "lock_table_shortfall",
    ):
        assert getattr(idb, name) is getattr(database, name)


def test_service_kit_does_not_import_pytest():
    import subprocess
    import sys

    code = "import sys, skeleton.testing, skeleton.testing.service_api; assert 'pytest' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_distributed_fixture_stops_with_a_usage_error_before_touching_the_database(monkeypatch):
    """Without the reset marker the fixture refuses before any connection (#334)."""
    import psycopg

    from skeleton.testing import database

    monkeypatch.delenv("SKELETON_TEST_ALLOW_SCHEMA_RESET", raising=False)
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: pytest.fail("the database was touched"))

    class Blocker:
        def unblock(self):
            from contextlib import nullcontext

            return nullcontext()

    generator = database.skeleton_db.__wrapped__(None, None, Blocker())
    with pytest.raises(pytest.UsageError, match="SKELETON_TEST_ALLOW_SCHEMA_RESET=1"):
        next(generator)


class _Blocker:
    def unblock(self):
        from contextlib import nullcontext

        return nullcontext()


@pytest.fixture
def fake_schema(monkeypatch):
    """The distributed fixture with every database step replaced by a recorder (#573)."""
    calls = []
    monkeypatch.setattr(kit, "_TEMPLATES", {})
    monkeypatch.setattr(kit, "SCHEMA_STATS", {"migrate": 0, "restore": 0})
    monkeypatch.setattr(kit, "_use_worker_database", lambda: calls.append("select"))
    monkeypatch.setattr(kit, "disposable_database_pair", lambda: PAIR)

    def migrate(pair):
        calls.append("migrate")
        kit.SCHEMA_STATS["migrate"] += 1

    monkeypatch.setattr(kit, "_migrate", migrate)
    monkeypatch.setattr(kit, "dump_schema", lambda pair: calls.append("dump") or kit.SchemaTemplate("", {}))
    monkeypatch.setattr(kit, "restore_schema", lambda pair, template: calls.append("restore"))
    monkeypatch.setattr(kit, "_configure", lambda config: config)
    monkeypatch.delenv(kit.TEMPLATE_DISABLE_ENV, raising=False)

    def run_tests(count, tmp_path):
        for _ in range(count):
            generator = kit.skeleton_db.__wrapped__(None, tmp_path, _Blocker())
            next(generator)
            with pytest.raises(StopIteration):
                next(generator)
        return calls

    return run_tests


def test_distributed_fixture_migrates_once_and_restores_each_test_with_pg_dump(fake_schema, monkeypatch, tmp_path):
    monkeypatch.setattr(kit, "pg_dump_executable", lambda: "/usr/bin/pg_dump")
    calls = fake_schema(3, tmp_path)
    assert [call for call in calls if call != "select"] == ["migrate", "dump", "restore", "restore", "restore"]
    assert kit.SCHEMA_STATS == {"migrate": 1, "restore": 3}


@pytest.mark.parametrize("disabled", ["pg_dump", "variable"])
def test_distributed_fixture_migrates_each_test_without_a_template(fake_schema, monkeypatch, tmp_path, disabled):
    if disabled == "pg_dump":
        monkeypatch.setattr(kit, "pg_dump_executable", lambda: None)
    else:
        monkeypatch.setattr(kit, "pg_dump_executable", lambda: "/usr/bin/pg_dump")
        monkeypatch.setenv(kit.TEMPLATE_DISABLE_ENV, "0")
    calls = fake_schema(3, tmp_path)
    assert [call for call in calls if call != "select"] == ["migrate", "migrate", "migrate"]
    assert kit.SCHEMA_STATS == {"migrate": 3, "restore": 0}


def test_distributed_fixture_turns_a_failed_dump_into_a_usage_error(fake_schema, monkeypatch, tmp_path):
    monkeypatch.setattr(kit, "pg_dump_executable", lambda: "/usr/bin/pg_dump")

    def failed(pair):
        raise RuntimeError("pg_dump of schema skeleton failed: version mismatch")

    monkeypatch.setattr(kit, "dump_schema", failed)
    with pytest.raises(pytest.UsageError, match="version mismatch"):
        fake_schema(1, tmp_path)


def _worker_environment(monkeypatch, worker="gw1"):
    monkeypatch.setenv(kit.RUNTIME_ENV, RUNTIME)
    monkeypatch.setenv(kit.OWNER_ENV, OWNER)
    monkeypatch.setenv(kit.WORKER_ENV, worker)
    monkeypatch.delenv(kit._SELECTED_ENV, raising=False)


def test_a_worker_that_only_imports_the_fixture_selects_its_database_once(monkeypatch):
    """A derived project has no pytest_configure of its own: the fixture selects the worker database (#573)."""
    import psycopg

    _worker_environment(monkeypatch)
    opened = []

    class Connection:
        def close(self):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda dsn, **kwargs: opened.append(dsn) or Connection())
    kit._use_worker_database()
    kit._use_worker_database()
    assert os_environ(kit.OWNER_ENV).endswith("/ci_gw1_skeleton_test")
    assert os_environ(kit.RUNTIME_ENV).endswith("/ci_gw1_skeleton_test")
    assert opened == [os_environ(kit.OWNER_ENV)], "selected and probed once, never derived twice"


def test_a_missing_worker_database_names_the_provisioning_command(monkeypatch):
    import psycopg

    _worker_environment(monkeypatch, "gw3")

    def refuse(dsn, **kwargs):
        raise psycopg.OperationalError('database "ci_gw3_skeleton_test" does not exist')

    monkeypatch.setattr(psycopg, "connect", refuse)
    for _ in range(2):  # Not selected after a failure: every test of the worker gets the same message.
        with pytest.raises(RuntimeError, match="ci_gw3_skeleton_test.*--workers N"):
            kit._use_worker_database()
        assert os_environ(kit.OWNER_ENV) == OWNER
        assert not kit.worker_database_selected()


def test_the_plugin_checks_every_worker_database_in_the_controller(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(kit, "worker_databases_problem", lambda count: f"{count} workers, ci_gw1_skeleton_test missing")
    controller = SimpleNamespace(option=SimpleNamespace(numprocesses=4, collectonly=False))
    with pytest.raises(pytest.UsageError, match="4 workers"):
        kit.pytest_configure(controller)
    monkeypatch.setattr(kit, "worker_databases_problem", lambda count: pytest.fail("checked without -n"))
    kit.pytest_configure(SimpleNamespace(option=SimpleNamespace(numprocesses=None, collectonly=False)))
    _worker_environment(monkeypatch, "gw2")
    kit.pytest_configure(SimpleNamespace(workerinput={}, option=SimpleNamespace()))
    assert os_environ(kit.OWNER_ENV).endswith("/ci_gw2_skeleton_test")


def test_worker_databases_problem_reports_missing_databases_before_the_lock_table(monkeypatch):
    monkeypatch.setenv(kit.RUNTIME_ENV, RUNTIME)
    monkeypatch.setenv(kit.OWNER_ENV, OWNER)
    monkeypatch.setattr(kit, "missing_worker_databases", lambda count: ["ci_gw1_skeleton_test"])
    monkeypatch.setattr(
        kit, "lock_table_shortfall", lambda count: pytest.fail("lock table read with a missing database")
    )
    problem = kit.worker_databases_problem(2)
    assert "ci_gw1_skeleton_test" in problem and "provision --database <base>_skeleton_test --workers 2" in problem
    monkeypatch.setattr(kit, "missing_worker_databases", lambda count: [])
    monkeypatch.setattr(kit, "lock_table_shortfall", lambda count: "raise max_locks_per_transaction")
    assert kit.worker_databases_problem(2) == "pytest -n 2: raise max_locks_per_transaction"
    monkeypatch.delenv(kit.OWNER_ENV)
    assert kit.worker_databases_problem(2) is None


# Reuse of the schema between tests (#760): the decisions, without a database.


@pytest.mark.parametrize(("value", "enabled"), [(None, True), ("1", True), ("", True), ("0", False)])
def test_schema_reuse_is_on_unless_switched_off(value, enabled):
    environment = {} if value is None else {idb.REUSE_ENV: value}
    assert idb.schema_reuse_enabled(environment) is enabled


def test_schema_reuse_verification_interval_comes_from_the_environment():
    assert idb.schema_reuse_verify_every({}) == idb.REUSE_VERIFY_DEFAULT > 0
    assert idb.schema_reuse_verify_every({idb.REUSE_VERIFY_ENV: "1"}) == 1
    assert idb.schema_reuse_verify_every({idb.REUSE_VERIFY_ENV: "0"}) == 0
    for value in ("-1", "often", "1.5", "1234567"):
        with pytest.raises(RuntimeError, match=idb.REUSE_VERIFY_ENV):
            idb.schema_reuse_verify_every({idb.REUSE_VERIFY_ENV: value})


def test_catalog_markers_cover_every_catalog_once_and_never_take_input():
    labels = [label for label, _ in idb._STABLE_CATALOGS + idb._VOLATILE_CATALOGS]
    assert len(labels) == len(set(labels))
    # The reuse rewrites only pg_class and pg_trigger rows (FORCE ROW LEVEL SECURITY, triggers).
    assert [label for label, _ in idb._VOLATILE_CATALOGS] == ["class", "trigger"]
    for query in (idb._REUSE_STABLE, idb._REUSE_VOLATILE, idb._REUSE_FLAGS, idb._REUSE_TABLES, idb._REUSE_SIZES):
        assert "%s" not in query and "{" not in query
        assert "'skeleton'" in query


class FakeReuse:
    def __init__(self):
        self.connection = FakeConnection()
        self.reused = 0
        self.reason = ""


@pytest.fixture
def fake_reuse(monkeypatch):
    """template_schema() with the database steps replaced by a log of what was asked."""
    log = []
    answers = {"reuse": True, "begin": True}
    monkeypatch.delenv(idb.REUSE_ENV, raising=False)
    monkeypatch.delenv(idb.REUSE_VERIFY_ENV, raising=False)
    monkeypatch.setattr(idb, "_REUSE", {})
    monkeypatch.setattr(idb, "restore_schema", lambda pair, template: log.append("restore"))

    def begin(pair):
        log.append("begin")
        return FakeReuse() if answers["begin"] else None

    def reuse(state, template, *, verify=False):
        log.append("reuse+verify" if verify else "reuse")
        state.reused += bool(answers["reuse"])
        return answers["reuse"]

    monkeypatch.setattr(idb, "begin_schema_reuse", begin)
    monkeypatch.setattr(idb, "reuse_schema", reuse)
    return log, answers


def test_first_test_recreates_the_schema_and_the_next_ones_reuse_it(fake_reuse):
    log, _ = fake_reuse
    assert [idb.template_schema(PAIR, "template") for _ in range(3)] == ["restore", "reuse", "reuse"]
    assert log == ["restore", "begin", "reuse", "reuse"]


def test_a_refused_reuse_recreates_the_schema_and_starts_over(fake_reuse):
    log, answers = fake_reuse
    idb.template_schema(PAIR, "template")
    refused = idb.current_schema_reuse(PAIR)
    answers["reuse"] = False

    assert idb.template_schema(PAIR, "template") == "restore"
    assert log == ["restore", "begin", "reuse", "restore", "begin"]
    assert refused.connection.closed, "The owner connection of a discarded state must not stay open."
    assert idb.current_schema_reuse(PAIR) is not refused

    answers["reuse"] = True
    assert idb.template_schema(PAIR, "template") == "reuse"


def test_every_nth_reuse_asks_for_the_whole_comparison(fake_reuse, monkeypatch):
    log, _ = fake_reuse
    monkeypatch.setenv(idb.REUSE_VERIFY_ENV, "3")
    for _ in range(7):
        idb.template_schema(PAIR, "template")
    assert log[2:] == ["reuse", "reuse", "reuse+verify", "reuse", "reuse", "reuse+verify"]
    del log[:]
    monkeypatch.setenv(idb.REUSE_VERIFY_ENV, "0")
    for _ in range(4):
        idb.template_schema(PAIR, "template")
    assert log == ["reuse"] * 4


def test_reuse_switched_off_never_keeps_a_state(fake_reuse, monkeypatch):
    log, _ = fake_reuse
    idb.template_schema(PAIR, "template")
    kept = idb.current_schema_reuse(PAIR)
    monkeypatch.setenv(idb.REUSE_ENV, "0")

    assert [idb.template_schema(PAIR, "template") for _ in range(2)] == ["restore", "restore"]
    assert log == ["restore", "begin", "restore", "restore"]
    assert kept.connection.closed and idb.current_schema_reuse(PAIR) is None


def test_a_schema_that_cannot_be_reused_is_recreated_for_every_test(fake_reuse):
    log, answers = fake_reuse
    answers["begin"] = False
    assert [idb.template_schema(PAIR, "template") for _ in range(2)] == ["restore", "restore"]
    assert log == ["restore", "begin", "restore", "begin"]


def test_each_worker_database_keeps_its_own_state(fake_reuse):
    log, _ = fake_reuse
    other = kit.DatabasePair(RUNTIME, OWNER, "ci_gw1_skeleton_test")
    assert idb.template_schema(PAIR, "template") == "restore"
    assert idb.template_schema(other, "template") == "restore"
    assert idb.template_schema(PAIR, "template") == "reuse"
    assert idb.current_schema_reuse(PAIR) is not idb.current_schema_reuse(other)


def test_reuse_reports_a_database_error_as_a_refusal_not_as_a_failure():
    import psycopg

    class Broken:
        def execute(self, *args, **kwargs):
            raise psycopg.OperationalError("the connection is closed")

    state = idb.SchemaReuse(
        pair=PAIR,
        connection=Broken(),
        tables=(),
        forced=frozenset(),
        seeded={},
        setvals="",
        flags=[],
        stable=[],
        volatile=[],
    )
    assert idb.reuse_schema(state, "template") is False
    assert state.reason == "OperationalError: the connection is closed" and state.reused == 0


def os_environ(name):
    import os

    return os.environ[name]
