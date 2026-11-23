"""Destructive integration setup is restricted to an explicitly disposable database."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
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
