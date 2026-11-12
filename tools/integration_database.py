"""Destructive integration setup is restricted to an explicitly disposable database."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict, make_conninfo

SUFFIX = "_skeleton_test"
RUNTIME_ENV = "TEST_DATABASE_URL"
OWNER_ENV = "TEST_MIGRATION_DATABASE_URL"
# pytest-xdist names its workers gw0, gw1, ...; each one gets a database of its own.
WORKER_ENV = "PYTEST_XDIST_WORKER"
_WORKER = re.compile(r"gw(0|[1-9][0-9]{0,2})")


@dataclass(frozen=True)
class DatabasePair:
    runtime: str
    owner: str
    database: str


def worker_database_name(database, worker):
    """Database of one parallel worker: ``ci_skeleton_test`` and ``gw3`` give ``ci_gw3_skeleton_test``.

    The name keeps the ``_skeleton_test`` suffix, so every barrier of
    ``disposable_database_pair()`` applies to it unchanged.
    """
    if not _WORKER.fullmatch(worker or ""):
        raise RuntimeError(f"Unexpected parallel worker id {worker!r}; expected gw0, gw1, ...")
    if not database or not database.endswith(SUFFIX) or database == SUFFIX:
        raise RuntimeError(f"Integration database name must end with {SUFFIX}.")
    return f"{database[: -len(SUFFIX)]}_{worker}{SUFFIX}"


def worker_database_url(url, worker):
    """The same URL (role, password, host, port, options) pointing at the worker's database."""
    parts = urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql"} or not parts.path.startswith("/"):
        raise RuntimeError("Parallel workers require postgres:// URLs that name a database.")
    return parts._replace(path="/" + worker_database_name(parts.path[1:], worker)).geturl()


def worker_database_urls(count, environment=None):
    """Runtime and owner URLs of the first ``count`` workers, from the two base variables."""
    environment = os.environ if environment is None else environment
    runtime, owner = environment.get(RUNTIME_ENV), environment.get(OWNER_ENV)
    if not runtime or not owner:
        raise RuntimeError(f"{RUNTIME_ENV} and {OWNER_ENV} are both required.")
    return [
        (worker_database_url(runtime, f"gw{index}"), worker_database_url(owner, f"gw{index}")) for index in range(count)
    ]


def select_worker_database(environment=None):
    """Point this process (a pytest-xdist worker) at its own disposable database.

    Rewrites both variables in place, so the settings, ``disposable_database_pair()``, tests
    that read the variables and the subprocesses they start all agree. Returns the worker's
    database name, or None outside a worker and when the variables are not set. Calling it
    twice is refused: the second call would derive a database from an already derived name.
    """
    environment = os.environ if environment is None else environment
    worker = environment.get(WORKER_ENV)
    runtime, owner = environment.get(RUNTIME_ENV), environment.get(OWNER_ENV)
    if not worker or not runtime or not owner:
        return None
    if environment.get(_SELECTED_ENV) == worker:
        raise RuntimeError("The worker database was already selected for this process.")
    environment[RUNTIME_ENV] = worker_database_url(runtime, worker)
    environment[OWNER_ENV] = worker_database_url(owner, worker)
    environment[_SELECTED_ENV] = worker
    return _identity(environment[RUNTIME_ENV])[2]


_SELECTED_ENV = "SKELETON_TEST_WORKER_DATABASE_SELECTED"


# restore_schema() drops and recreates every object of schema skeleton in one transaction and
# holds one lock per object until the commit: about 2,100 today, counted with pg_locks.
LOCKS_PER_RESTORE = 3000
_LOCK_ADVICE = (
    "raise max_locks_per_transaction and restart the server (`tools/dev_postgres.py start` does "
    "it for the local one), or run fewer parallel workers"
)


def lock_table_shortfall(count, environment=None):
    """None when ``count`` workers can restore at once; otherwise the message that says what to raise.

    PostgreSQL shares one lock table of max_locks_per_transaction x (max_connections +
    max_prepared_transactions) slots among all sessions; any role may read the three settings.
    """
    import psycopg

    _, owner = worker_database_urls(1, environment)[0]
    with psycopg.connect(owner, connect_timeout=10) as connection:
        per_transaction, sessions = connection.execute(
            "SELECT current_setting('max_locks_per_transaction')::int, "
            "current_setting('max_connections')::int + current_setting('max_prepared_transactions')::int"
        ).fetchone()
    needed = count * LOCKS_PER_RESTORE
    if per_transaction * sessions >= needed:
        return None
    return (
        f"{count} workers recreating schema skeleton at once need about {needed} lock slots and the server "
        f"has {per_transaction * sessions} (max_locks_per_transaction={per_transaction}): set it to at least "
        f"{-(-needed // sessions)}. To fix it, {_LOCK_ADVICE}."
    )


def missing_worker_databases(count, environment=None):
    """Names of the worker databases that the owner cannot open, in worker order.

    Read-only: it connects and disconnects. The fixture never creates a database or a role;
    provisioning them is the operator's step (``tools/dev_postgres.py start`` or the CI).
    """
    import psycopg

    missing = []
    for _, owner in worker_database_urls(count, environment):
        try:
            psycopg.connect(owner, connect_timeout=10).close()
        except psycopg.OperationalError:
            missing.append(_identity(owner)[2])
    return missing


def _identity(dsn):
    values = conninfo_to_dict(dsn)
    database = values.get("dbname")
    if not database:
        raise RuntimeError("Integration DSNs must explicitly name a database.")
    return (values.get("host", ""), values.get("port", "5432"), database)


def disposable_database_pair():
    if os.environ.get("SKELETON_TEST_ALLOW_SCHEMA_RESET") != "1":
        raise RuntimeError("Set SKELETON_TEST_ALLOW_SCHEMA_RESET=1 for a disposable integration database.")
    runtime = os.environ.get(RUNTIME_ENV)
    owner = os.environ.get(OWNER_ENV)
    if not runtime or not owner:
        raise RuntimeError(f"{RUNTIME_ENV} and {OWNER_ENV} are both required.")
    identity = _identity(runtime)
    if identity != _identity(owner):
        raise RuntimeError("Runtime and migration DSNs must target the same host, port and database.")
    if not identity[2].endswith(SUFFIX):
        raise RuntimeError(f"Integration database name must end with {SUFFIX}.")
    worker = os.environ.get(WORKER_ENV)
    if worker and os.environ.get(_SELECTED_ENV) != worker:
        # Two workers on one database would recreate each other's schema mid-test.
        raise RuntimeError("A parallel worker must select its own database before using it.")
    for name in ("DATABASE_URL", "MIGRATION_DATABASE_URL"):
        configured = os.environ.get(name)
        if configured and _identity(configured) == identity:
            raise RuntimeError(f"Integration database must differ from {name}.")
    if conninfo_to_dict(runtime).get("user") != "skeleton_runtime":
        raise RuntimeError("Assertions require the skeleton_runtime role, not an owner or superuser.")
    if conninfo_to_dict(owner).get("user") != "skeleton_owner":
        raise RuntimeError("Schema setup requires the dedicated skeleton_owner role.")
    return DatabasePair(runtime, owner, identity[2])


@dataclass(frozen=True)
class SchemaTemplate:
    """Plain SQL of a migrated schema skeleton plus the catalog fingerprint it was taken from."""

    sql: str
    fingerprint: dict


def _verify_owner(connection, pair):
    """The connected role is the validated, unprivileged owner of the disposable database."""
    if not isinstance(pair, DatabasePair):
        raise RuntimeError("Schema operations require the pair returned by disposable_database_pair().")
    identity = connection.execute("SELECT current_database(), current_user").fetchone()
    if identity != (pair.database, "skeleton_owner"):
        raise RuntimeError("Connected migration identity differs from the validated DSN.")
    role = connection.execute(
        "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    if role is None or any(role):
        raise RuntimeError("Integration migrator must not be a PostgreSQL superuser/role/database creator.")


def _owner_connection(pair):
    import psycopg

    if not isinstance(pair, DatabasePair):
        raise RuntimeError("Schema operations require the pair returned by disposable_database_pair().")
    connection = psycopg.connect(pair.owner, autocommit=True)
    try:
        _verify_owner(connection, pair)
    except BaseException:
        connection.close()
        raise
    return connection


def _create_empty_schema(connection):
    connection.execute("DROP SCHEMA IF EXISTS skeleton CASCADE")
    connection.execute("CREATE SCHEMA skeleton AUTHORIZATION skeleton_owner")
    connection.execute("REVOKE ALL ON SCHEMA skeleton FROM PUBLIC")
    connection.execute("GRANT USAGE ON SCHEMA skeleton TO skeleton_runtime, skeleton_audit_writer")


def reset_schema(pair):
    """Do not create a database/role or touch anything outside schema skeleton."""
    with _owner_connection(pair) as connection:
        _create_empty_schema(connection)


# Template of the migrated schema: migrate once per test session, restore it for each test.
# The restore recreates schema skeleton from scratch as skeleton_owner, so every test still
# starts from a fresh schema; nothing is flushed or truncated.

TEMPLATE_DISABLE_ENV = "SKELETON_TEST_POSTGRES_TEMPLATE"
PG_DUMP_ENV = "SKELETON_TEST_PG_DUMP"
_CREATE_SCHEMA = "CREATE SCHEMA skeleton;"
# pg_dump >= 16.10/17.6 brackets plain output in psql-only \restrict/\unrestrict lines.
_RESTRICT = re.compile(r"^\\(un)?restrict [A-Za-z0-9]+$")


def template_enabled():
    return os.environ.get(TEMPLATE_DISABLE_ENV, "1") != "0"


def _pg_dump_command(pair):
    """Argv and env for pg_dump; the password travels in PGPASSWORD, never in argv."""
    executable = shutil.which(os.environ.get(PG_DUMP_ENV, "pg_dump"))
    if executable is None:
        raise RuntimeError(
            "pg_dump not found: install a PostgreSQL client matching the server, point "
            f"{PG_DUMP_ENV} to it, or set {TEMPLATE_DISABLE_ENV}=0 to migrate for each test."
        )
    params = conninfo_to_dict(pair.owner)
    password = params.pop("password", None)
    env = dict(os.environ)
    env.pop("PGPASSWORD", None)
    if password is not None:
        env["PGPASSWORD"] = password
    argv = [
        executable,
        "--dbname",
        make_conninfo(**params),
        "--schema=skeleton",
        "--format=plain",
        # INSERTs run through psycopg; COPY FROM stdin would need psql.
        "--column-inserts",
        "--rows-per-insert=500",
        "--no-password",
    ]
    return argv, env


def prepare_dump(text):
    """Strip psql meta-commands and the CREATE SCHEMA the restore issues itself."""
    lines = [line for line in text.splitlines() if not _RESTRICT.match(line)]
    meta = [line for line in lines if line.startswith("\\")]
    if meta:
        raise RuntimeError(f"Unexpected psql meta-command in schema dump: {meta[0][:40]!r}")
    if lines.count(_CREATE_SCHEMA) != 1:
        raise RuntimeError("Schema dump must contain exactly one CREATE SCHEMA skeleton statement.")
    lines.remove(_CREATE_SCHEMA)
    return "\n".join(lines) + "\n"


def dump_schema(pair):
    """Capture structure and data of the migrated schema skeleton, as skeleton_owner."""
    argv, env = _pg_dump_command(pair)
    with _owner_connection(pair) as connection:
        fingerprint = _fingerprint(connection)
    result = subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump of schema skeleton failed: {result.stderr.strip()}")
    return SchemaTemplate(sql=prepare_dump(result.stdout), fingerprint=fingerprint)


def restore_schema(pair, template):
    """Recreate schema skeleton from the template in one transaction, as skeleton_owner."""
    if not isinstance(template, SchemaTemplate):
        raise RuntimeError("restore_schema() requires the SchemaTemplate returned by dump_schema().")
    import psycopg

    try:
        with _owner_connection(pair) as connection, connection.transaction():
            # A lost fixture schema is rebuilt by the next test: do not wait for the WAL flush.
            connection.execute("SET LOCAL synchronous_commit = off")
            _create_empty_schema(connection)
            # Audit functions are owned by skeleton_audit_writer; like InstallRecordInfrastructure,
            # the ownership transfer needs schema CREATE only for the duration of the DDL.
            connection.execute("GRANT CREATE ON SCHEMA skeleton TO skeleton_audit_writer")
            # No parameters: psycopg sends the whole script with the simple query protocol.
            connection.execute(template.sql)
            connection.execute("REVOKE CREATE ON SCHEMA skeleton FROM skeleton_audit_writer")
    except psycopg.errors.OutOfMemory as exc:
        # "out of shared memory": the lock table is full, usually with several workers restoring.
        raise RuntimeError(
            f"PostgreSQL ran out of lock slots while recreating schema skeleton: {_LOCK_ADVICE}."
        ) from exc


# Cache of the template between local runs (#350). Migrating costs 4 to 8 s per process and per
# worker, and between two runs of the suite usually only tests changed. The cache is keyed by
# everything that shapes the migrated schema; any difference migrates again. The CI disables it
# (SKELETON_TEST_TEMPLATE_CACHE=0) so that every run proves the migrations.

CACHE_ENV = "SKELETON_TEST_TEMPLATE_CACHE"
CACHE_FORMAT = 1
CACHE_ENTRIES_KEPT = 4
ROOT = Path(__file__).resolve().parents[1]
# Code and data that migrations, post_migrate handlers, specs and settings are made of. Broad on
# purpose: an edit outside tests/ costs one migrate, a missed input would cost a wrong schema.
_KEY_TREES = ("skeleton", "config", "applications", "examples")
_KEY_FILES = ("tools/integration_database.py", "tests/conftest.py")
_KEY_SUFFIXES = {".py", ".json", ".sql", ".toml", ".cfg", ".txt"}


def template_cache_dir(environment=None):
    """Directory of the cache, or None when ``SKELETON_TEST_TEMPLATE_CACHE=0`` disables it."""
    environment = os.environ if environment is None else environment
    value = environment.get(CACHE_ENV, "")
    if value == "0":
        return None
    return Path(value) if value else ROOT / "var" / "test-template-cache"


def _source_files(root):
    for tree in _KEY_TREES:
        base = root / tree
        if base.is_dir():
            for path in base.rglob("*"):
                if path.suffix in _KEY_SUFFIXES and "__pycache__" not in path.parts and path.is_file():
                    yield path
    for name in _KEY_FILES:
        if (root / name).is_file():
            yield root / name


def template_cache_key(
    *, server_version, pg_dump_version, installed_apps, migration_modules, environment=None, root=None
):
    """Hash of every input that shapes the migrated schema. Values are hashed, never stored."""
    from importlib import metadata

    environment = os.environ if environment is None else environment
    root = ROOT if root is None else Path(root)
    digest = hashlib.sha256()

    def add(label, value):
        digest.update(f"{label}\0{len(value)}\0".encode())
        digest.update(value if isinstance(value, bytes) else value.encode())

    add("format", str(CACHE_FORMAT))
    add("server", str(server_version))
    add("pg_dump", str(pg_dump_version))
    add("apps", json.dumps(list(installed_apps)))
    add("migration_modules", json.dumps(dict(migration_modules), sort_keys=True))
    # The settings module and the project's own variables (SKELETON_ENABLE_EXAMPLE_APP,
    # SKELETON_BUNDLED_APPS...); the test harness variables (URLs, worker ids) do not shape it.
    for name in sorted(environment):
        if name == "DJANGO_SETTINGS_MODULE" or (name.startswith("SKELETON_") and not name.startswith("SKELETON_TEST_")):
            add("env:" + name, environment[name])
    # Django and every library with migrations or DDL of their own.
    for name, version in sorted({(dist.metadata["Name"] or "", dist.version) for dist in metadata.distributions()}):
        add("dist:" + name, version)
    for path in sorted(_source_files(root), key=lambda item: item.relative_to(root).as_posix()):
        add("file:" + path.relative_to(root).as_posix(), path.read_bytes())
    return digest.hexdigest()


def _rows(fingerprint):
    """The fingerprint as _fingerprint() builds it: lists of row tuples (JSON keeps lists)."""
    return {section: [tuple(row) for row in rows] for section, rows in fingerprint.items()}


def load_cached_template(directory, key):
    """The template stored under ``key``, or None when it is missing, foreign or malformed."""
    if directory is None or not re.fullmatch(r"[0-9a-f]{64}", key or ""):
        return None
    try:
        stored = json.loads((Path(directory) / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or stored.get("format") != CACHE_FORMAT or stored.get("key") != key:
        return None
    sql, fingerprint = stored.get("sql"), stored.get("fingerprint")
    if not isinstance(sql, str) or not isinstance(fingerprint, dict):
        return None
    # The same guards as a fresh dump: no psql meta-command, no CREATE SCHEMA of its own.
    if any(line.startswith("\\") for line in sql.splitlines()) or _CREATE_SCHEMA in sql.splitlines():
        return None
    if not all(isinstance(rows, list) and all(isinstance(row, list) for row in rows) for rows in fingerprint.values()):
        return None
    return SchemaTemplate(sql=sql, fingerprint=_rows(fingerprint))


def save_cached_template(directory, key, template):
    """Store the template atomically; a failure leaves the cache as it was (it is only a cache)."""
    if directory is None or not re.fullmatch(r"[0-9a-f]{64}", key or ""):
        return False
    payload = {"format": CACHE_FORMAT, "key": key, "sql": template.sql, "fingerprint": template.fingerprint}
    try:
        exact = _rows(json.loads(json.dumps(payload["fingerprint"]))) == template.fingerprint
    except (TypeError, ValueError, AttributeError):
        exact = False
    if not exact:
        return False  # A value JSON cannot round-trip: do not store an approximation.
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, prefix=".tmp-", suffix=".json", delete=False
        ) as handle:
            json.dump(payload, handle)
        # Workers may store the same key at once: os.replace keeps one complete file.
        os.replace(handle.name, directory / f"{key}.json")
    except OSError:
        try:
            os.unlink(handle.name)
        except (OSError, UnboundLocalError):
            pass
        return False
    entries = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in entries[CACHE_ENTRIES_KEPT:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return True


def pg_dump_version():
    executable = shutil.which(os.environ.get(PG_DUMP_ENV, "pg_dump"))
    if executable is None:
        return ""
    result = subprocess.run([executable, "--version"], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def server_version(pair):
    with _owner_connection(pair) as connection:
        return connection.execute("SELECT current_setting('server_version_num')").fetchone()[0]


def schema_fingerprint(pair):
    with _owner_connection(pair) as connection:
        return _fingerprint(connection)


# A NULL ACL means the default privileges. REVOKE can leave an explicit ACL equal to that
# default, which pg_dump omits, so the restore yields NULL again: compare acldefault() instead.
_FINGERPRINT_QUERIES = {
    "schema": """
        SELECT pg_get_userbyid(nspowner), ARRAY(SELECT unnest(coalesce(nspacl, acldefault('n', nspowner)))::text ORDER BY 1)
        FROM pg_namespace WHERE nspname='skeleton'""",
    "relations": """
        SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner), c.relrowsecurity, c.relforcerowsecurity,
               ARRAY(SELECT unnest(coalesce(c.relacl, CASE
                   WHEN c.relkind = 'S' THEN acldefault('s', c.relowner)
                   WHEN c.relkind IN ('r', 'p', 'v', 'm', 'f') THEN acldefault('r', c.relowner)
               END))::text ORDER BY 1)
        FROM pg_class c WHERE c.relnamespace='skeleton'::regnamespace ORDER BY 1""",
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attidentity,
               a.attgenerated, pg_get_expr(d.adbin, d.adrelid), ARRAY(SELECT unnest(a.attacl)::text ORDER BY 1)
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE c.relnamespace='skeleton'::regnamespace AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY 1, 2""",
    "constraints": """
        SELECT coalesce(c.relname, ''), k.conname, k.contype, pg_get_constraintdef(k.oid), k.condeferrable, k.condeferred
        FROM pg_constraint k LEFT JOIN pg_class c ON c.oid=k.conrelid
        WHERE k.connamespace='skeleton'::regnamespace ORDER BY 1, 2""",
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname='skeleton' ORDER BY 1, 2",
    "triggers": """
        SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid), t.tgenabled
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        WHERE c.relnamespace='skeleton'::regnamespace AND NOT t.tgisinternal ORDER BY 1, 2""",
    "policies": """
        SELECT tablename, policyname, permissive, ARRAY(SELECT unnest(roles)::text ORDER BY 1), cmd, qual, with_check
        FROM pg_policies WHERE schemaname='skeleton' ORDER BY 1, 2""",
    "functions": """
        SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_function_result(p.oid),
               pg_get_userbyid(p.proowner), p.prokind, p.prosecdef, p.provolatile, p.proconfig,
               ARRAY(SELECT unnest(coalesce(p.proacl, acldefault('f', p.proowner)))::text ORDER BY 1), md5(p.prosrc)
        FROM pg_proc p WHERE p.pronamespace='skeleton'::regnamespace ORDER BY 1, 2""",
    "sequences": "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname='skeleton' ORDER BY 1",
}


def _fingerprint(connection):
    """Owner-side inventory of schema skeleton: DDL, ownership, ACLs, RLS flags and row data."""
    from psycopg import sql

    result = {name: connection.execute(query).fetchall() for name, query in _FINGERPRINT_QUERIES.items()}
    result["constraints"] = _canonical_checks(connection, result["constraints"])
    result["indexes"] = _canonical_indexes(connection, result["indexes"])
    tables = connection.execute(
        "SELECT relname FROM pg_class WHERE relnamespace='skeleton'::regnamespace AND relkind IN ('r','p') ORDER BY 1"
    ).fetchall()
    result["data"] = [
        (
            table,
            *connection.execute(
                sql.SQL(
                    "SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) FROM skeleton.{} AS t"
                ).format(sql.Identifier(table))
            ).fetchone(),
        )
        for (table,) in tables
    ]
    return result


_CHECK_PROBE = "sk_fingerprint_probe"


def _canonical_checks(connection, rows):
    """CHECK text as PostgreSQL deparses it after one dump/restore round trip.

    pg_dump writes the deparsed CHECK and the restore parses it again, which can move casts
    (``IN`` over varchar) without changing the expression. Re-adding each expression as a
    NOT VALID probe inside a rolled-back transaction yields that same round-tripped text on
    both sides; every other column is compared unchanged.
    """
    from psycopg import sql

    canonical = []
    with connection.transaction(force_rollback=True):
        for table, name, kind, definition, deferrable, deferred in rows:
            if kind == "c" and table:
                expression = definition.removesuffix(" NOT VALID")
                target = sql.Identifier("skeleton", table)
                connection.execute(
                    sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} {} NOT VALID").format(
                        target, sql.Identifier(_CHECK_PROBE), sql.SQL(expression)
                    )
                )
                (probe,) = connection.execute(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=%s::regclass AND conname=%s",
                    (target.as_string(connection), _CHECK_PROBE),
                ).fetchone()
                connection.execute(
                    sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(target, sql.Identifier(_CHECK_PROBE))
                )
                suffix = definition[len(expression) :]
                definition = probe.removesuffix(" NOT VALID") + suffix
            canonical.append((table, name, kind, definition, deferrable, deferred))
    return canonical


_INDEX_NAME = re.compile(r"^(CREATE (?:UNIQUE )?INDEX )(\S+)( ON )")


def _canonical_indexes(connection, rows):
    """Index definitions with predicates or expressions after the same round trip as CHECKs.

    Partial indexes such as ``WHERE type IN (...)`` over varchar deparse differently once
    restored. Each one is rebuilt under a probe name in a rolled-back transaction and its
    definition is read back with the original name; plain column indexes are kept as they are.
    """
    from psycopg import sql

    computed = {
        name
        for (name,) in connection.execute(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "WHERE c.relnamespace='skeleton'::regnamespace AND (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL)"
        ).fetchall()
    }
    canonical = []
    with connection.transaction(force_rollback=True):
        for table, name, definition in rows:
            match = _INDEX_NAME.match(definition)
            if name in computed and match:
                probe_name = sql.Identifier(_CHECK_PROBE).as_string(connection)
                connection.execute(sql.SQL(_INDEX_NAME.sub(rf"\g<1>{probe_name}\g<3>", definition, count=1)))
                (probe,) = connection.execute(
                    "SELECT pg_get_indexdef(%s::regclass)", (f"skeleton.{probe_name}",)
                ).fetchone()
                connection.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier("skeleton", _CHECK_PROBE)))
                original = match[2]
                definition = _INDEX_NAME.sub(
                    lambda found, original=original: found[1] + original + found[3], probe, count=1
                )
            canonical.append((table, name, definition))
    return canonical
