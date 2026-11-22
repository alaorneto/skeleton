"""PostgreSQL checks of the setup: the runtime role, the transient owner and the migration.

The runtime DSN is stored (encrypted) in the instance SQLite only after it proves to be an
unprivileged role: no superuser, no BYPASSRLS, not the owner of the ``skeleton`` schema and
without CREATE on it. The owner DSN lives only in the memory of the request that migrates:
it is never stored, logged or kept in the session.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from django.utils.translation import gettext

SCHEMA = "skeleton"
CONNECT_TIMEOUT = 5
DEFAULT_PORT = 5432
logger = logging.getLogger("skeleton.setup")


class DatabaseCheckError(Exception):
    """A DSN that the setup refuses; the message is shown to the operator (no secrets).

    ``code`` names the cause and ``field`` the form field it belongs to (``None``: the whole form),
    so the setup shows the message next to what the operator has to fix (#464).
    """

    def __init__(self, message: str, *, code: str = "invalid", field: str | None = None):
        super().__init__(message)
        self.code = code
        self.field = field


def build_dsn(*, host: str, port: int | str, name: str, user: str, password: str) -> str:
    """postgres:// URL from separate fields; every part is percent-encoded."""
    host = host.strip()
    if ":" in host and not host.startswith("["):  # IPv6 literal
        host = f"[{host}]"
    credentials = quote(user, safe="") + (":" + quote(password, safe="") if password else "")
    return f"postgres://{credentials}@{host}:{int(port)}/{quote(name, safe='')}"


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


def _network_problem(host: str, port: int) -> DatabaseCheckError | None:
    """Name resolution and TCP reachability, tested before the driver to tell the causes apart.

    Host and port were typed by the operator on the same screen and may appear in the message.
    """
    if not host or host.startswith("/"):  # A Unix socket: nothing to resolve.
        return None
    values = {"host": host, "port": port, "seconds": CONNECT_TIMEOUT}
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return DatabaseCheckError(
            gettext(
                "O servidor “%(host)s” não foi encontrado. Confira o nome. Dentro do Docker, use o nome do serviço"
                " (por exemplo postgres), não localhost."
            )
            % values,
            code="dns",
            field="db_host",
        )
    try:
        probe = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except ConnectionRefusedError:
        return DatabaseCheckError(
            gettext(
                "Conexão recusada em %(host)s:%(port)s. O PostgreSQL não está escutando nesse endereço e porta, ou"
                " um firewall bloqueia."
            )
            % values,
            code="refused",
            field="db_port",
        )
    except TimeoutError:
        return DatabaseCheckError(
            gettext(
                "O servidor %(host)s:%(port)s não respondeu em %(seconds)s segundos. Confira o endereço, a porta e"
                " o firewall."
            )
            % values,
            code="timeout",
            field="db_host",
        )
    except OSError as exc:
        return DatabaseCheckError(
            gettext("Não foi possível alcançar %(host)s:%(port)s (%(error_class)s).")
            % {**values, "error_class": type(exc).__name__},
            code="network",
            field="db_host",
        )
    probe.close()
    return None


# Connection failures carry no SQLSTATE in psycopg 3 (libpq reports them as text), so the English
# server text is the fallback; it is only matched, never shown or logged.
AUTH_STATES = {"28P01", "28000"}
AUTH_TEXTS = ("password authentication failed", "authentication failed", "no password supplied")


def _driver_problem(exc: Exception) -> DatabaseCheckError:
    sqlstate = getattr(exc, "sqlstate", None)
    text = str(exc).lower()
    if (
        sqlstate in AUTH_STATES
        or any(part in text for part in AUTH_TEXTS)
        or ("role" in text and "does not exist" in text)
    ):
        return DatabaseCheckError(
            gettext(
                "O PostgreSQL recusou o usuário ou a senha. Confira o nome da role e a senha definida para ela (não é"
                " a senha do superusuário postgres)."
            ),
            code="auth",
            field="db_password",
        )
    if sqlstate == "3D000" or ("database" in text and "does not exist" in text):
        return DatabaseCheckError(
            gettext("O banco informado não existe nesse servidor. Confira o nome do banco."),
            code="database_missing",
            field="db_name",
        )
    if sqlstate == "53300" or "too many connections" in text:
        return DatabaseCheckError(
            gettext("O servidor recusou a conexão por excesso de conexões abertas. Tente de novo em instantes."),
            code="too_many_connections",
        )
    return DatabaseCheckError(
        gettext("Não foi possível conectar (%(__name__)s). Veja o log skeleton.setup.")
        % {"__name__": type(exc).__name__},
        code="connect_failed",
    )


def hosts_of(config: dict) -> list[tuple[str, int]]:
    """Every host and port of a DSN; ``h1:5432,h2:5432`` is a list for failover (#676).

    One port for every host is accepted, as in libpq; a different count of hosts and ports is refused.
    """
    hosts = [item.strip() for item in str(config.get("HOST") or "").split(",")]
    raw_ports = [item.strip() for item in str(config.get("PORT") or "").split(",")]
    if raw_ports == [""]:
        raw_ports = [str(DEFAULT_PORT)]
    if len(raw_ports) == 1:
        raw_ports *= len(hosts)
    if len(raw_ports) != len(hosts) or not all(port.isdigit() for port in raw_ports):
        raise DatabaseCheckError(gettext("A lista de portas não corresponde à lista de servidores."), code="ports")
    return [(host, int(port)) for host, port in zip(hosts, raw_ports, strict=True)]


# libpq parameters a DSN query may carry to the setup's connection (sslmode, failover).
CONNECT_OPTIONS = ("sslmode", "sslrootcert", "sslcert", "sslkey", "target_session_attrs")


def _connect(dsn: str):
    import psycopg

    config = parse(dsn)
    problems = [_network_problem(host, port) for host, port in hosts_of(config)]
    # With several hosts, one that answers is enough: libpq tries them in order (#676).
    problem = None if any(item is None for item in problems) else problems[0]
    sqlstate, error_class = None, None
    if problem is None:
        extra = {key: value for key, value in (config.get("OPTIONS") or {}).items() if key in CONNECT_OPTIONS}
        try:
            return psycopg.connect(
                dbname=config["NAME"],
                user=config["USER"],
                password=config.get("PASSWORD") or None,
                host=config.get("HOST") or None,
                port=config.get("PORT") or None,
                connect_timeout=CONNECT_TIMEOUT,
                options=f"-c search_path={SCHEMA},public",
                **extra,
            )
        except psycopg.Error as exc:
            # Never echo the driver message: it can carry the host and the user.
            problem = _driver_problem(exc)
            sqlstate, error_class = getattr(exc, "sqlstate", None), type(exc).__name__
    logger.warning(
        "setup.database_connect_failed code=%s error_class=%s sqlstate=%s", problem.code, error_class, sqlstate
    )
    raise problem from None


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
                "O banco existe, mas ainda não foi preparado: o schema skeleton não existe. Rode uma vez, com um"
                ' usuário administrador do PostgreSQL: psql "$ADMIN_DSN" -v ON_ERROR_STOP=1 -f'
                " deploy/postgres/bootstrap.sql. Depois defina as senhas com \\password skeleton_owner e"
                " \\password skeleton_runtime e volte aqui."
            ),
            code="schema_missing",
        )
    if role.superuser:
        raise DatabaseCheckError(
            gettext(
                "Esta role é superusuário. Use a role de aplicação (skeleton_runtime), que não pode ter privilégios"
                " de administrador."
            ),
            code="superuser",
            field="db_user",
        )
    if role.bypass_rls:
        raise DatabaseCheckError(
            gettext(
                "Esta role tem BYPASSRLS e ignoraria o isolamento entre organizações. Use a role de aplicação"
                " (skeleton_runtime). A role skeleton_owner vai na próxima etapa."
            ),
            code="bypassrls",
            field="db_user",
        )
    if role.owns_schema or role.schema_create:
        raise DatabaseCheckError(
            gettext(
                "Esta role é dona do schema skeleton ou pode criar tabelas nele. Use a role de aplicação"
                " (skeleton_runtime)."
            ),
            code="owns_schema",
            field="db_user",
        )
    return role


def validate_owner(owner_dsn: str, runtime: Role) -> Role:
    """The transient migration owner: same database as the runtime, a different role."""
    owner = inspect(owner_dsn)
    if (owner.database, owner.host, owner.port) != (runtime.database, runtime.host, runtime.port):
        raise DatabaseCheckError(
            gettext("A credencial de migração precisa apontar para o mesmo banco da role runtime."),
            code="owner_other_database",
        )
    if owner.name == runtime.name:
        raise DatabaseCheckError(
            gettext("Esta é a role de aplicação. Aqui vai a role dona do schema (skeleton_owner)."),
            code="owner_is_runtime",
            field="owner_user",
        )
    if not owner.owns_schema:
        raise DatabaseCheckError(
            gettext("Esta role não é dona do schema skeleton. Use skeleton_owner."),
            code="owner_not_owner",
            field="owner_user",
        )
    if owner.superuser:
        raise DatabaseCheckError(
            gettext("Use a role dona do schema (skeleton_owner), não um superuser."),
            code="owner_superuser",
            field="owner_user",
        )
    return owner


@contextmanager
def django_alias(alias: str, dsn: str) -> Iterator[str]:
    """The ``default`` connection of this thread on ``dsn`` for one request, then the original back.

    Setup mode runs with ``DATABASES = {}``, so ``default`` is Django's dummy backend. Code that
    reads through it without a ``using`` (the unique checks of ``full_clean`` when the catalog is
    synced, the identity bootstrap) needs a real connection there, and the catalog rows are keyed
    by the alias the plain ``migrate`` uses. So the whole step runs on ``default``: this thread's
    connection object is swapped and restored, never the shared ``DATABASES`` entry (#451).
    """
    from django.db import connections
    from django.db.utils import ConnectionHandler, load_backend

    if alias != "default":
        raise ValueError("The setup steps run on the default alias.")
    config = parse(dsn)
    config.setdefault("OPTIONS", {})["options"] = f"-c search_path={SCHEMA},public"
    config["ATOMIC_REQUESTS"] = False
    config["CONN_MAX_AGE"] = 0
    normalized = ConnectionHandler({"default": config}).settings["default"]
    original = connections["default"]
    original.close()
    connections["default"] = load_backend(normalized["ENGINE"]).DatabaseWrapper(normalized, "default")
    try:
        yield alias
    finally:
        connections["default"].close()
        connections["default"] = original


def agent_manages_schema(environment=None) -> bool:
    """True when the deployment agent migrated the schema already (``SKELETON_SETUP_SCHEMA=agent``, #652).

    A container deployment installs the core through the agent, which applies the migrations with
    the owner on its first start. The schema step then only confirms the schema with the runtime
    role and never asks for the owner credential.
    """
    import os

    environment = os.environ if environment is None else environment
    return environment.get("SKELETON_SETUP_SCHEMA", "").lower() == "agent"


def confirm_schema(runtime_dsn: str) -> list[str]:
    """The deployed-schema check with the runtime role (the agent migrated); returns problems."""
    from skeleton.system.checks import check_deployed_schema

    with django_alias("default", runtime_dsn) as alias:
        return check_deployed_schema(using=alias)


def migrate_schema(owner_dsn: str) -> list[str]:
    """``migrate``, ``sync_table_catalog`` and the schema check as the owner; returns problems."""
    from io import StringIO

    from django.core.management import call_command

    from skeleton.system.checks import check_deployed_schema
    from skeleton.system.synchronization import sync_table_catalog

    # The owner credential lives only on this thread's default connection, and only for this call.
    with django_alias("default", owner_dsn) as alias:
        call_command("migrate", database=alias, interactive=False, verbosity=0, stdout=StringIO())
        sync_table_catalog(using=alias)
        return check_deployed_schema(using=alias)
