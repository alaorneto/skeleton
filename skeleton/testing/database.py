"""Disposable PostgreSQL database for the tests of a derived project (#334).

The barriers are the core suite's own (``tools/integration_database.py`` re-exports them): a
destructive reset runs only with ``SKELETON_TEST_ALLOW_SCHEMA_RESET=1``, on a database whose
name ends with ``_skeleton_test``, distinct from ``DATABASE_URL``, through the dedicated
``skeleton_owner`` role; every assertion runs as ``skeleton_runtime``.

``skeleton_db`` recreates schema ``skeleton`` and migrates it for each test (never a flush or a
TRUNCATE). It is the simple path: the core suite keeps its faster schema template. This module
imports pytest; the rest of ``skeleton.testing`` does not.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import environ  # type: ignore[import-untyped]
import pytest
from psycopg.conninfo import conninfo_to_dict

SUFFIX = "_skeleton_test"
RUNTIME_ENV = "TEST_DATABASE_URL"
OWNER_ENV = "TEST_MIGRATION_DATABASE_URL"
# pytest-xdist names its workers gw0, gw1, ...; each one gets a database of its own.
WORKER_ENV = "PYTEST_XDIST_WORKER"
_SELECTED_ENV = "SKELETON_TEST_WORKER_DATABASE_SELECTED"


@dataclass(frozen=True)
class DatabasePair:
    runtime: str
    owner: str
    database: str


def _identity(dsn: str) -> tuple[Any, Any, Any]:
    values = conninfo_to_dict(dsn)
    database = values.get("dbname")
    if not database:
        raise RuntimeError("Integration DSNs must explicitly name a database.")
    return (values.get("host", ""), values.get("port", "5432"), database)


def disposable_database_pair() -> DatabasePair:
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


def _verify_owner(connection: Any, pair: DatabasePair) -> None:
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


def _owner_connection(pair: DatabasePair) -> Any:
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


def _create_empty_schema(connection: Any) -> None:
    connection.execute("DROP SCHEMA IF EXISTS skeleton CASCADE")
    connection.execute("CREATE SCHEMA skeleton AUTHORIZATION skeleton_owner")
    connection.execute("REVOKE ALL ON SCHEMA skeleton FROM PUBLIC")
    connection.execute("GRANT USAGE ON SCHEMA skeleton TO skeleton_runtime, skeleton_audit_writer")


def reset_schema(pair: DatabasePair) -> None:
    """Do not create a database/role or touch anything outside schema skeleton."""
    with _owner_connection(pair) as connection:
        _create_empty_schema(connection)


def _configure(config: dict[str, Any]) -> dict[str, Any]:
    from django.conf import settings
    from django.db import connections
    from django.db.utils import ConnectionHandler

    connections.close_all()
    normalized: dict[str, Any] = ConnectionHandler({"default": config}).settings["default"]
    settings.DATABASES["default"] = normalized
    connections.databases["default"] = normalized
    connections["default"].settings_dict = normalized
    return normalized


def _postgres_config(dsn: str) -> dict[str, Any]:
    config: dict[str, Any] = environ.Env.db_url_config(dsn)
    config["OPTIONS"] = {"options": "-c search_path=skeleton,public"}
    return config


@pytest.fixture
def skeleton_db(request: pytest.FixtureRequest, tmp_path: Path, django_db_blocker: Any) -> Iterator[None]:
    """Per-test database: schema skeleton recreated and migrated by skeleton_owner, assertions as skeleton_runtime.

    Apps start closed (#22): ``build_service_scenario`` makes the model's app available and active.
    Missing variables or a database that is not disposable stop the run with a usage error.
    """
    from django.contrib.contenttypes.models import ContentType
    from django.core.management import call_command
    from django.db import connections
    from django.test import override_settings

    original = connections["default"].settings_dict.copy()
    with django_db_blocker.unblock():
        try:
            pair = disposable_database_pair()
        except RuntimeError as exc:
            raise pytest.UsageError(str(exc)) from exc
        reset_schema(pair)
        _configure(_postgres_config(pair.owner))
        call_command("migrate", verbosity=0, interactive=False)
        connections.close_all()
        ContentType.objects.clear_cache()
        _configure(_postgres_config(pair.runtime))
        try:
            # A locmem cache per test: never clear a configured external cache or share rate limits.
            with override_settings(
                CACHES={
                    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": str(tmp_path)}
                }
            ):
                yield
        finally:
            connections.close_all()
            _configure(original)


# Provisioning of a disposable test database (#520): the wheel ships a byte-for-byte copy of
# deploy/postgres/bootstrap.sql, so the CI of an app in its own repository needs only the wheel.
BOOTSTRAP_SQL = Path(__file__).resolve().with_name("bootstrap.sql")
ADMIN_URL_ENV = "SKELETON_TEST_ADMIN_DATABASE_URL"
OWNER_PASSWORD_ENV = "SKELETON_TEST_OWNER_PASSWORD"  # noqa: S105 - name of a variable, not a password
RUNTIME_PASSWORD_ENV = "SKELETON_TEST_RUNTIME_PASSWORD"  # noqa: S105 - name of a variable, not a password
_DATABASE_NAME = r"[a-z_][a-z0-9_]{0,62}"


def provision(admin_url: str, database: str, *, passwords: dict[str, str] | None = None) -> bool:
    """Create ``database`` if missing, apply the bootstrap SQL and grant CREATE to the owner.

    Idempotent. ``admin_url`` is an operator connection allowed to create databases and roles; it is
    never printed or stored. ``passwords`` maps ``skeleton_owner``/``skeleton_runtime`` to the
    passwords of the disposable roles. Returns whether the database was created.
    """
    import re

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    if not re.fullmatch(_DATABASE_NAME, database) or not database.endswith(SUFFIX):
        raise RuntimeError(f"The test database name must be a plain identifier ending with {SUFFIX}.")
    if not admin_url:
        raise RuntimeError(f"An administrative URL is required (--admin-url or {ADMIN_URL_ENV}).")
    with psycopg.connect(admin_url, autocommit=True) as connection:
        exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
        if not exists:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    with psycopg.connect(make_conninfo(admin_url, dbname=database), autocommit=True) as connection:
        connection.execute(BOOTSTRAP_SQL.read_text(encoding="utf-8"))
        connection.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO skeleton_owner").format(sql.Identifier(database)))
        for role, password in (passwords or {}).items():
            if role not in ("skeleton_owner", "skeleton_runtime"):
                raise RuntimeError(f"Unknown role {role!r}.")
            if password:
                connection.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
                )
    return not exists


def main(argv: list[str] | None = None) -> int:
    """``python -m skeleton.testing.database provision --database <name>_skeleton_test``."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m skeleton.testing.database", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("provision", help="Create and bootstrap a disposable *_skeleton_test database.")
    command.add_argument("--admin-url", help=f"Operator connection URL (default: ${ADMIN_URL_ENV}). Never printed.")
    command.add_argument("--database", required=True, help=f"Database name; must end with {SUFFIX}.")
    options = parser.parse_args(argv)
    passwords = {
        "skeleton_owner": os.environ.get(OWNER_PASSWORD_ENV, ""),
        "skeleton_runtime": os.environ.get(RUNTIME_PASSWORD_ENV, ""),
    }
    try:
        created = provision(
            options.admin_url or os.environ.get(ADMIN_URL_ENV, ""), options.database, passwords=passwords
        )
    except RuntimeError as exc:
        parser.error(str(exc))
    except Exception as exc:  # noqa: BLE001 - a driver error must not echo the administrative URL
        parser.exit(1, f"provision failed: {exc.__class__.__name__}\n")
    print(f"{options.database}: {'created' if created else 'already existed'}, bootstrap applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
