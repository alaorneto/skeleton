"""Destructive integration setup is restricted to an explicitly disposable database."""

import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# The barriers of a destructive reset (#334), the worker databases and the schema template
# (#573) live in the wheel, for derived projects; the core suite re-exports them. Only the cache
# of the template between runs (#350) is the suite's own.
from skeleton.testing.database import (  # noqa: F401 - re-exported for the core suite
    _CREATE_SCHEMA,
    _LOCK_ADVICE,
    _SELECTED_ENV,
    LOCKS_PER_RESTORE,
    OWNER_ENV,
    PG_DUMP_ENV,
    RUNTIME_ENV,
    SUFFIX,
    TEMPLATE_DISABLE_ENV,
    WORKER_ENV,
    DatabasePair,
    SchemaTemplate,
    _canonical_checks,
    _canonical_indexes,
    _create_empty_schema,
    _fingerprint,
    _identity,
    _owner_connection,
    _pg_dump_command,
    _verify_owner,
    disposable_database_pair,
    dump_schema,
    lock_table_shortfall,
    missing_worker_databases,
    prepare_dump,
    reset_schema,
    restore_schema,
    schema_fingerprint,
    select_worker_database,
    template_enabled,
    worker_database_name,
    worker_database_url,
    worker_database_urls,
)

# Cache of the template between local runs (#350). Migrating costs about 20 s per process, and
# between two runs of the suite usually only tests changed. The cache is keyed by everything
# that shapes the migrated schema; any difference migrates again. The CI points it at a directory
# of the job, empty at the start, so that every run proves the migrations once; with
# SKELETON_TEST_TEMPLATE_CACHE=0 every process migrates.

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
    kept = {path.stem for path in entries[:CACHE_ENTRIES_KEPT]}
    # Old entries, and the lock files of builds whose entry is gone or was never stored.
    stale = entries[CACHE_ENTRIES_KEPT:] + [path for path in directory.glob("*.lock") if path.stem not in kept]
    for path in stale:
        try:
            path.unlink()
        except OSError:
            pass  # Only a cache; on Windows a lock file that is still held cannot be removed.
    return True


# One build per run (#756). With a cold cache every pytest-xdist worker used to migrate its own
# database at the same time; now the first one builds and stores the template and the others
# wait and read it. The lock is the operating system's, on an open file: it goes away with the
# process that holds it, so a killed run never blocks the next one.

BUILD_LOCK_TIMEOUT = 600.0
_BUILD_LOCK_POLL = 0.1


def _try_lock(handle):
    """Take the exclusive lock of an open file without waiting; False when another process has it."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle):
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # Closing the file releases it anyway.


@contextmanager
def template_build_lock(directory, key, *, timeout=BUILD_LOCK_TIMEOUT):
    """Hold the build of the template of ``key``: one process migrates, the others wait here.

    Yields True when this process holds the lock. Yields False, without waiting, when the cache
    is disabled or the key is not a digest, and after ``timeout`` seconds or when the lock file
    cannot be opened: the caller then builds its own template, as before the lock existed. The
    caller reads the cache again once inside, because the holder it waited for has stored it.
    """
    if directory is None or not re.fullmatch(r"[0-9a-f]{64}", key or ""):
        yield False
        return
    try:
        Path(directory).mkdir(parents=True, exist_ok=True)
        handle = open(Path(directory) / f"{key}.lock", "a+b")  # noqa: SIM115 - held across the yield
    except OSError:
        yield False
        return
    try:
        deadline = time.monotonic() + timeout
        held = _try_lock(handle)
        while not held and time.monotonic() < deadline:
            time.sleep(_BUILD_LOCK_POLL)
            held = _try_lock(handle)
        try:
            yield held
        finally:
            if held:
                _unlock(handle)
    finally:
        handle.close()


def pg_dump_version():
    executable = shutil.which(os.environ.get(PG_DUMP_ENV, "pg_dump"))
    if executable is None:
        return ""
    result = subprocess.run([executable, "--version"], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def server_version(pair):
    with _owner_connection(pair) as connection:
        return connection.execute("SELECT current_setting('server_version_num')").fetchone()[0]


# Reuse of the schema between tests (#760). Recreating schema skeleton costs about 760 files
# created and 760 removed, and that, not the CPU, bounds the suite. Almost no test changes the
# structure of the schema: tests write rows. After a real restore the suite remembers what the
# template holds; before the next test it deletes, as skeleton_owner, the rows written since then
# and checks that nothing else changed. A change to the catalog, a change to a row the
# migrations seeded, an error or a doubt gives the real restore, as before. No assertion of a
# test runs here, and a test still starts from the schema the migrations produce.

REUSE_ENV = "SKELETON_TEST_SCHEMA_REUSE"
REUSE_VERIFY_ENV = "SKELETON_TEST_SCHEMA_REUSE_VERIFY"
# One reuse in this many is compared with the whole fingerprint of the template (0: never).
REUSE_VERIFY_DEFAULT = 25
REUSE_LOCK_TIMEOUT = "5s"
# Deleted rows leave dead tuples; a table that grew this much past the template is vacuumed.
_REUSE_VACUUM_BYTES = 4 * 1024 * 1024

# Every query below is built only from the constants of this module (S608: no input reaches them).
_NAMESPACE = "(SELECT oid FROM pg_namespace WHERE nspname = 'skeleton')"
_RELATIONS = f"(SELECT oid FROM pg_class WHERE relnamespace = {_NAMESPACE})"  # noqa: S608 - constants
_TYPES = f"(SELECT oid FROM pg_type WHERE typnamespace = {_NAMESPACE})"  # noqa: S608 - constants
# Catalogs the reuse itself never writes: they must be exactly as the last restore left them.
_STABLE_CATALOGS = (
    ("attrdef", f"pg_attrdef WHERE adrelid IN {_RELATIONS}"),
    ("attribute", f"pg_attribute WHERE attrelid IN {_RELATIONS}"),
    ("constraint", f"pg_constraint WHERE connamespace = {_NAMESPACE}"),
    ("default_acl", f"pg_default_acl WHERE defaclnamespace = {_NAMESPACE}"),
    # Every object of a schema, of any kind, depends on it: a table, a type, an operator, a collation...
    ("depend", f"pg_depend WHERE refclassid = 'pg_namespace'::regclass AND refobjid = {_NAMESPACE}"),
    ("enum", f"pg_enum WHERE enumtypid IN {_TYPES}"),
    ("index", f"pg_index WHERE indrelid IN {_RELATIONS}"),
    ("inherits", f"pg_inherits WHERE inhrelid IN {_RELATIONS}"),
    ("namespace", "pg_namespace WHERE nspname = 'skeleton'"),
    ("policy", f"pg_policy WHERE polrelid IN {_RELATIONS}"),
    ("proc", f"pg_proc WHERE pronamespace = {_NAMESPACE}"),
    ("rewrite", f"pg_rewrite WHERE ev_class IN {_RELATIONS}"),
    ("sequence", f"pg_sequence WHERE seqrelid IN {_RELATIONS}"),
    ("type", f"pg_type WHERE typnamespace = {_NAMESPACE}"),
)
# Catalogs the reuse writes, lifting and putting back FORCE ROW LEVEL SECURITY and the triggers:
# they must be as the previous reuse left them, and that state is checked by _REUSE_FLAGS.
_VOLATILE_CATALOGS = (
    ("class", f"pg_class WHERE relnamespace = {_NAMESPACE}"),
    ("trigger", f"pg_trigger WHERE tgrelid IN {_RELATIONS}"),
)


def _catalog_marker(catalogs):
    """One row per catalog: how many rows of schema skeleton it has and which transactions wrote them.

    Every DDL statement inserts, updates or deletes catalog rows, and a new or updated row carries
    the id of its transaction (xmin): the same count, maximum and sum mean no statement touched it.
    """
    selects = [
        f"SELECT '{label}', count(*), max(xmin::text::bigint), sum(xmin::text::bigint)::text FROM {source}"  # noqa: S608 - constants
        for label, source in catalogs
    ]
    return " UNION ALL ".join(selects) + " ORDER BY 1"


_REUSE_STABLE = _catalog_marker(_STABLE_CATALOGS)
_REUSE_VOLATILE = _catalog_marker(_VOLATILE_CATALOGS)
_REUSE_FLAGS = f"""
    SELECT 'rls', c.relname::text, c.relrowsecurity::text || ':' || c.relforcerowsecurity::text
    FROM pg_class c WHERE c.relnamespace = {_NAMESPACE} AND c.relkind IN ('r', 'p')
    UNION ALL
    SELECT 'trigger', c.relname::text, t.tgname::text || ':' || t.tgenabled::text
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
    WHERE c.relnamespace = {_NAMESPACE} AND NOT t.tgisinternal
    ORDER BY 1, 2, 3"""  # noqa: S608 - constants
_REUSE_TABLES = f"""
    SELECT c.relname::text, c.relkind::text, pg_get_userbyid(c.relowner) = current_user, c.relforcerowsecurity,
           CASE WHEN c.relkind = 'r' THEN pg_relation_size(c.oid) ELSE 0 END,
           (SELECT a.attname::text FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
            WHERE i.indrelid = c.oid AND i.indisprimary AND i.indnatts = 1)
    FROM pg_class c WHERE c.relnamespace = {_NAMESPACE} AND c.relkind IN ('r', 'p', 'm', 'f')
    ORDER BY 1"""  # noqa: S608 - constants
_REUSE_SIZES = f"""
    SELECT c.relname::text, pg_relation_size(c.oid) FROM pg_class c
    WHERE c.relnamespace = {_NAMESPACE} AND c.relkind = 'r' AND pg_relation_size(c.oid) > 0
    ORDER BY 1"""  # noqa: S608 - constants
_REUSE_SEQUENCES = f"""
    SELECT c.relname::text FROM pg_class c
    WHERE c.relnamespace = {_NAMESPACE} AND c.relkind = 'S'
    ORDER BY 1"""  # noqa: S608 - constants
_ROWS_DIGEST = (
    "SELECT {}, count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) FROM skeleton.{} AS t"
)


def schema_reuse_enabled(environment=None):
    """False with ``SKELETON_TEST_SCHEMA_REUSE=0``: every test recreates the schema."""
    return (os.environ if environment is None else environment).get(REUSE_ENV, "1") != "0"


def schema_reuse_verify_every(environment=None):
    """How often a reuse is compared with the whole fingerprint: every Nth, 0 for never."""
    value = (os.environ if environment is None else environment).get(REUSE_VERIFY_ENV, "")
    if not value:
        return REUSE_VERIFY_DEFAULT
    if not re.fullmatch(r"[0-9]{1,6}", value):
        raise RuntimeError(f"{REUSE_VERIFY_ENV} must be a number of reuses between checks (0 disables): {value!r}.")
    return int(value)


class _NotReusable(Exception):
    """The schema is not at the template after the deletes: roll back and restore it for real."""


@dataclass
class SchemaReuse:
    """What the template holds, read right after a real restore, and the owner connection kept for it."""

    pair: DatabasePair
    connection: object
    tables: tuple
    forced: frozenset
    seeded: dict  # table -> (primary key column, keys of the template rows, digest of them, heap size)
    setvals: str
    flags: list
    stable: list
    volatile: list
    lock_timeout: str = REUSE_LOCK_TIMEOUT
    reused: int = 0
    reason: str = ""


def _table(name):
    from psycopg import sql

    return sql.SQL("skeleton.{}").format(sql.Identifier(name))


def _alter_tables(connection, statement, names):
    from psycopg import sql

    if names:
        # No parameters: one round trip with the simple query protocol, as the restore does.
        connection.execute(sql.SQL("").join(sql.SQL(statement).format(_table(name)) for name in names))


def _rows_digest(connection, names):
    from psycopg import sql

    if not names:
        return {}
    query = sql.SQL(" UNION ALL ").join(
        sql.SQL(_ROWS_DIGEST).format(sql.Literal(name), sql.Identifier(name)) for name in names
    )
    return {name: (count, digest) for name, count, digest in connection.execute(query).fetchall()}


def begin_schema_reuse(pair):
    """Right after restore_schema(): what to compare the schema with before the next tests.

    Returns None when this schema cannot be reused safely (a table of another owner, a
    materialized, partitioned or foreign table, a trigger that is not simply enabled, seeded rows
    without a single-column primary key): every test then gets the real restore.
    """
    from psycopg import sql

    connection = _owner_connection(pair)
    # A long-lived connection: no server-side prepared plan to go stale.
    connection.prepare_threshold = None
    try:
        tables = connection.execute(_REUSE_TABLES).fetchall()
        flags = connection.execute(_REUSE_FLAGS).fetchall()
        obstacles = (
            [
                f"{name} is not a plain table of the owner"
                for name, kind, owned, _, _, _ in tables
                if kind != "r" or not owned
            ]
            + [
                f"trigger {value[:-2]} of {name} is not enabled"
                for kind, name, value in flags
                if kind == "trigger" and not value.endswith(":O")
            ]
            + [
                f"{name} has seeded rows and no single-column primary key"
                for name, _, _, _, size, key in tables
                if size > 0 and key is None
            ]
        )
        if obstacles:
            _REUSE_UNSUPPORTED[pair.database] = obstacles[0]
            connection.close()
            return None
        _REUSE_UNSUPPORTED.pop(pair.database, None)
        forced = frozenset(name for name, _, _, force, _, _ in tables if force)
        seeded = {}
        # Rows are read with FORCE ROW LEVEL SECURITY lifted in a transaction that is rolled back.
        with connection.transaction(force_rollback=True):
            names = [name for name, _, _, _, size, _ in tables if size > 0]
            _alter_tables(connection, "ALTER TABLE {} NO FORCE ROW LEVEL SECURITY;", [n for n in names if n in forced])
            digests = _rows_digest(connection, names)
            for name, _, _, _, size, key in tables:
                if size > 0:
                    keys = [
                        row[0]
                        for row in connection.execute(
                            sql.SQL("SELECT {}::text FROM {}").format(sql.Identifier(key), _table(name))
                        )
                    ]
                    seeded[name] = (key, keys, digests[name], size)
        sequences = [row[0] for row in connection.execute(_REUSE_SEQUENCES)]
        setvals = []
        for name in sequences:
            value, called = connection.execute(
                sql.SQL("SELECT last_value, is_called FROM {}").format(_table(name))
            ).fetchone()
            setvals.append(
                sql.SQL("setval({}::regclass, {}, {})").format(
                    sql.Literal(_table(name).as_string(connection)), sql.Literal(value), sql.Literal(called)
                )
            )
        return SchemaReuse(
            pair=pair,
            connection=connection,
            tables=tuple(name for name, *_ in tables),
            forced=forced,
            seeded=seeded,
            setvals=(sql.SQL("SELECT ") + sql.SQL(", ").join(setvals)).as_string(connection) if setvals else "SELECT 1",
            flags=flags,
            stable=connection.execute(_REUSE_STABLE).fetchall(),
            volatile=connection.execute(_REUSE_VOLATILE).fetchall(),
        )
    except BaseException:
        connection.close()
        raise


def end_schema_reuse(state):
    if state is not None:
        try:
            state.connection.close()
        except Exception:  # noqa: BLE001, S110 - closing a connection that may already be gone
            pass


def rows_outside_template(state):
    """Tables that hold a row the template does not have, read without row level security."""
    connection = state.connection
    with connection.transaction(force_rollback=True):
        _alter_tables(connection, "ALTER TABLE {} NO FORCE ROW LEVEL SECURITY;", sorted(state.forced))
        digests = _rows_digest(connection, state.tables)
    expected = {name: value[2] for name, value in state.seeded.items()}
    return sorted(
        name
        for name, (count, digest) in digests.items()
        if (count, digest) != expected.get(name, (0, digests[name][1]))
    )


def _delete_written_rows(state, dirty):
    """Inside the transaction: delete what is not of the template and put the sequences back."""
    from psycopg import sql

    connection = state.connection
    forced = [name for name in dirty if name in state.forced]
    _alter_tables(connection, "ALTER TABLE {} NO FORCE ROW LEVEL SECURITY;", forced)
    _alter_tables(connection, "ALTER TABLE {} DISABLE TRIGGER USER;", dirty)
    deletes, parameters = [], []
    for position, name in enumerate(dirty):
        statement = sql.SQL("DELETE FROM {}").format(_table(name))
        if name in state.seeded:
            key, keys, _, _ = state.seeded[name]
            statement += sql.SQL(" WHERE {}::text <> ALL(%s::text[])").format(sql.Identifier(key))
            parameters.append(keys)
        deletes.append(sql.SQL("{} AS ({})").format(sql.Identifier(f"deleted_{position}"), statement))
    if deletes:
        # One statement: a foreign key that is not deferrable is checked when all rows are gone.
        connection.execute(sql.SQL("WITH {} SELECT 1").format(sql.SQL(", ").join(deletes)), parameters)
    connection.execute(state.setvals)
    # Deferred foreign key checks run now: ALTER TABLE refuses a table with pending trigger events.
    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
    seeded = {name: value[2] for name, value in state.seeded.items()}
    if _rows_digest(connection, list(seeded)) != seeded:
        raise _NotReusable("a row seeded by the migrations changed")
    _alter_tables(connection, "ALTER TABLE {} ENABLE TRIGGER USER;", dirty)
    _alter_tables(connection, "ALTER TABLE {} FORCE ROW LEVEL SECURITY;", forced)


def _reuse(state, template, verify):
    from psycopg import sql

    connection = state.connection
    if connection.execute(_REUSE_STABLE).fetchall() != state.stable:
        raise _NotReusable("the structure of the schema changed")
    if connection.execute(_REUSE_VOLATILE).fetchall() != state.volatile:
        raise _NotReusable("a table, sequence, grant or trigger of the schema changed")
    sizes = connection.execute(_REUSE_SIZES).fetchall()
    with connection.transaction():
        # A lost fixture schema is rebuilt by the next test: do not wait for the WAL flush.
        connection.execute("SET LOCAL synchronous_commit = off")
        connection.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(state.lock_timeout)))
        _delete_written_rows(state, [name for name, _ in sizes])
    if connection.execute(_REUSE_FLAGS).fetchall() != state.flags:
        raise _NotReusable("row level security or a trigger is not as in the template")
    for name, size in sizes:
        if size - (state.seeded[name][3] if name in state.seeded else 0) > _REUSE_VACUUM_BYTES:
            connection.execute(sql.SQL("VACUUM {}").format(_table(name)))
    if verify:
        # The whole comparison of the template tests, and every table read without RLS.
        leftovers = rows_outside_template(state)
        if leftovers or _fingerprint(connection) != template.fingerprint:
            raise RuntimeError(
                "The reused schema skeleton differs from the template"
                + (f" (rows left in {', '.join(leftovers[:5])})" if leftovers else "")
                + f". This is a defect of the schema reuse (#760): report it and run with {REUSE_ENV}=0 meanwhile."
            )
    state.volatile = connection.execute(_REUSE_VOLATILE).fetchall()


def reuse_schema(state, template, *, verify=False):
    """Bring a schema whose structure did not change back to the template; False asks for the restore.

    As skeleton_owner, in one transaction: lift FORCE ROW LEVEL SECURITY and the user triggers of
    the tables whose file grew, delete their rows (in the tables the migrations seed, only the
    rows the template does not have, and what stays must be the template's), put the sequences
    back and restore the flags and the triggers. A failure rolls everything back: the schema is
    never left half clean, and the caller recreates it.
    """
    import psycopg

    state.reason = ""
    try:
        _reuse(state, template, verify)
    except _NotReusable as exc:
        state.reason = str(exc)
    except psycopg.Error as exc:
        # A lock held by a connection the test left behind, a connection that was terminated...
        state.reason = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
    else:
        state.reused += 1
        return True
    return False


_REUSE = {}
# Why the schema of a database cannot be reused at all (begin_schema_reuse returned None).
_REUSE_UNSUPPORTED = {}
# How many times this process recreated a schema it had kept, by the reason of the refusal.
_REUSE_REFUSALS = {}


def current_schema_reuse(pair):
    """The state of the reuse of this process for the database of ``pair``, or None."""
    return _REUSE.get(pair.database)


def _forget_schema_reuse(database):
    end_schema_reuse(_REUSE.pop(database, None))


def template_schema(pair, template):
    """Leave schema skeleton at the template for the next test: returns "reuse" or "restore"."""
    state = _REUSE.get(pair.database)
    if state is not None and schema_reuse_enabled():
        every = schema_reuse_verify_every()
        if reuse_schema(state, template, verify=bool(every) and (state.reused + 1) % every == 0):
            return "reuse"
        _REUSE_REFUSALS[state.reason] = _REUSE_REFUSALS.get(state.reason, 0) + 1
    _forget_schema_reuse(pair.database)
    restore_schema(pair, template)
    if schema_reuse_enabled():
        state = begin_schema_reuse(pair)
        if state is not None:
            _REUSE[pair.database] = state
    return "restore"


@atexit.register
def _close_schema_reuse():
    for database in list(_REUSE):
        _forget_schema_reuse(database)


def schema_reuse_obstacles():
    """Why this process recreates the schema for every test although the reuse is on, if it does."""
    return sorted(set(_REUSE_UNSUPPORTED.values()))


def schema_reuse_refusals():
    """Reason of each refused reuse of this process and how many times it happened."""
    return dict(_REUSE_REFUSALS)


def schema_summary(stats, obstacles=(), refusals=None):
    """The end of a run: how its schemas were built and why some were recreated; None without database tests."""
    if not any(stats.get(key) for key in ("migrate", "cached", "restore", "reuse")):
        return None
    line = (
        f"skeleton_db schemas: {stats.get('migrate', 0)} migrated, {stats.get('cached', 0)} from the template cache, "
        f"{stats.get('restore', 0)} recreated, {stats.get('reuse', 0)} reused"
    )
    if obstacles:
        line += "; schema reuse unavailable: " + "; ".join(obstacles)
    if refusals:
        ordered = sorted(refusals.items(), key=lambda item: (-item[1], item[0]))
        line += "\nskeleton_db recreated a kept schema because: " + "; ".join(
            f"{reason} ({count})" for reason, count in ordered
        )
    return line
