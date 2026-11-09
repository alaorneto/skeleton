"""Destructive integration setup is restricted to an explicitly disposable database."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from psycopg.conninfo import conninfo_to_dict, make_conninfo


@dataclass(frozen=True)
class DatabasePair:
    runtime: str
    owner: str
    database: str


def _identity(dsn):
    values = conninfo_to_dict(dsn)
    database = values.get("dbname")
    if not database:
        raise RuntimeError("Integration DSNs must explicitly name a database.")
    return (values.get("host", ""), values.get("port", "5432"), database)


def disposable_database_pair():
    if os.environ.get("SKELETON_TEST_ALLOW_SCHEMA_RESET") != "1":
        raise RuntimeError("Set SKELETON_TEST_ALLOW_SCHEMA_RESET=1 for a disposable integration database.")
    runtime = os.environ.get("TEST_DATABASE_URL")
    owner = os.environ.get("TEST_MIGRATION_DATABASE_URL")
    if not runtime or not owner:
        raise RuntimeError("TEST_DATABASE_URL and TEST_MIGRATION_DATABASE_URL are both required.")
    identity = _identity(runtime)
    if identity != _identity(owner):
        raise RuntimeError("Runtime and migration DSNs must target the same host, port and database.")
    if not identity[2].endswith("_skeleton_test"):
        raise RuntimeError("Integration database name must end with _skeleton_test.")
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
    with _owner_connection(pair) as connection, connection.transaction():
        _create_empty_schema(connection)
        # Audit functions are owned by skeleton_audit_writer; like InstallRecordInfrastructure,
        # the ownership transfer needs schema CREATE only for the duration of the DDL.
        connection.execute("GRANT CREATE ON SCHEMA skeleton TO skeleton_audit_writer")
        # No parameters: psycopg sends the whole script with the simple query protocol.
        connection.execute(template.sql)
        connection.execute("REVOKE CREATE ON SCHEMA skeleton FROM skeleton_audit_writer")


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
