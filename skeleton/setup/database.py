"""PostgreSQL checks of the setup: the runtime role, the transient owner and the migration.

The runtime DSN is stored (encrypted) in the instance SQLite only after it proves to be an
unprivileged role: no superuser, no BYPASSRLS, not the owner of the ``skeleton`` schema and
without CREATE on it. The owner DSN lives only in the memory of the request that migrates:
it is never stored, logged or kept in the session.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.utils.translation import gettext

SCHEMA = "skeleton"
CONNECT_TIMEOUT = 5


class DatabaseCheckError(Exception):
    """A DSN that the setup refuses; the message is shown to the operator (no secrets)."""


@dataclass(frozen=True)
class Role:
    name: str
    database: str
    host: str
    port: str
    superuser: bool
    bypass_rls: bool
    owns_schema: bool
    schema_create: bool
    schema_exists: bool


def parse(dsn: str) -> dict:
    if not isinstance(dsn, str) or urlsplit(dsn).scheme not in ("postgres", "postgresql"):
        raise DatabaseCheckError(gettext("Use uma URL postgres://usuário:senha@host:porta/banco."))
    try:
        import environ  # type: ignore[import-untyped]

        config = environ.Env.db_url_config(dsn)
    except Exception:  # noqa: BLE001 - any parser failure is the same invalid-URL refusal
        raise DatabaseCheckError(gettext("URL de banco inválida.")) from None
    if not config.get("NAME") or not config.get("USER"):
        raise DatabaseCheckError(gettext("Informe usuário e banco na URL."))
    return config


def _connect(dsn: str):
    import psycopg

    config = parse(dsn)
    try:
        return psycopg.connect(
            dbname=config["NAME"],
            user=config["USER"],
            password=config.get("PASSWORD") or None,
            host=config.get("HOST") or None,
            port=config.get("PORT") or None,
            connect_timeout=CONNECT_TIMEOUT,
            options=f"-c search_path={SCHEMA},public",
        )
    except psycopg.Error as exc:
        # Never echo the driver message: it can carry the host and the user.
        raise DatabaseCheckError(
            gettext("Não foi possível conectar (%(__name__)s).") % {"__name__": type(exc).__name__}
        ) from None


def inspect(dsn: str) -> Role:
    config = parse(dsn)
    with _connect(dsn) as connection:
        row = connection.execute(
            "SELECT current_user, current_database(), r.rolsuper, r.rolbypassrls,"
            " n.nspowner IS NOT NULL AND pg_has_role(current_user, n.nspowner, 'USAGE'),"
            " n.oid IS NOT NULL AND has_schema_privilege(current_user, n.oid, 'CREATE'),"
            " n.oid IS NOT NULL"
            " FROM pg_roles r LEFT JOIN pg_namespace n ON n.nspname = %s WHERE r.rolname = current_user",
            [SCHEMA],
        ).fetchone()
    return Role(
        name=row[0],
        database=row[1],
        host=str(config.get("HOST") or ""),
        port=str(config.get("PORT") or ""),
        superuser=row[2],
        bypass_rls=row[3],
        owns_schema=bool(row[4]),
        schema_create=bool(row[5]),
        schema_exists=bool(row[6]),
    )


def validate_runtime(dsn: str) -> Role:
    """The runtime role that the web and the workers use; refuses any privileged role."""
    role = inspect(dsn)
    if not role.schema_exists:
        raise DatabaseCheckError(
            gettext(
                "O schema skeleton não existe: provisione o banco com deploy/postgres/bootstrap.sql antes do setup."
            )
        )
    if role.superuser:
        raise DatabaseCheckError(gettext("A role runtime não pode ser superuser."))
    if role.bypass_rls:
        raise DatabaseCheckError(
            gettext("A role runtime não pode ter BYPASSRLS: a RLS deixaria de isolar organizações.")
        )
    if role.owns_schema or role.schema_create:
        raise DatabaseCheckError(gettext("A role runtime não pode ser dona do schema skeleton nem ter CREATE nele."))
    return role


def validate_owner(owner_dsn: str, runtime: Role) -> Role:
    """The transient migration owner: same database as the runtime, a different role."""
    owner = inspect(owner_dsn)
    if (owner.database, owner.host, owner.port) != (runtime.database, runtime.host, runtime.port):
        raise DatabaseCheckError(
            gettext("A credencial de migração precisa apontar para o mesmo banco da role runtime.")
        )
    if owner.name == runtime.name:
        raise DatabaseCheckError(gettext("A credencial de migração precisa ser outra role, dona do schema skeleton."))
    if not owner.owns_schema:
        raise DatabaseCheckError(gettext("A credencial de migração precisa ser dona do schema skeleton."))
    if owner.superuser:
        raise DatabaseCheckError(gettext("Use a role dona do schema (skeleton_owner), não um superuser."))
    return owner


@contextmanager
def django_alias(alias: str, dsn: str) -> Iterator[str]:
    """A temporary ``DATABASES`` alias for one request; removed (and closed) afterwards."""
    from django.db import connections
    from django.db.utils import ConnectionHandler

    config = parse(dsn)
    config.setdefault("OPTIONS", {})["options"] = f"-c search_path={SCHEMA},public"
    config["ATOMIC_REQUESTS"] = False
    config["CONN_MAX_AGE"] = 0
    normalized = ConnectionHandler({"default": config}).settings["default"]
    if alias == "default":
        # Code that reads through the default connection (identity bootstrap) needs the runtime
        # role there; setup mode has only the dummy alias, which comes back afterwards.
        original = connections.databases.get("default")
        connections["default"].close()
        connections.databases["default"] = normalized
        connections["default"].settings_dict = normalized
        try:
            yield alias
        finally:
            connections["default"].close()
            connections.databases["default"] = original
            connections["default"].settings_dict = original
        return
    connections.databases[alias] = normalized
    try:
        yield alias
    finally:
        if alias in connections:
            connections[alias].close()
            del connections[alias]
        connections.databases.pop(alias, None)


def migrate_schema(owner_dsn: str) -> list[str]:
    """``migrate``, ``sync_table_catalog`` and the schema check as the owner; returns problems."""
    from io import StringIO

    from django.core.management import call_command

    from skeleton.system.checks import check_deployed_schema
    from skeleton.system.synchronization import sync_table_catalog

    with django_alias("setup_owner", owner_dsn) as alias:
        call_command("migrate", database=alias, interactive=False, verbosity=0, stdout=StringIO())
        sync_table_catalog(using=alias)
        return check_deployed_schema(using=alias)
