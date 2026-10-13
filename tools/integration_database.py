"""Destructive integration setup is restricted to an explicitly disposable database."""
from dataclasses import dataclass
import os

from psycopg.conninfo import conninfo_to_dict


@dataclass(frozen=True)
class DatabasePair:
    runtime: str
    owner: str
    database: str


def _identity(dsn):
    values = conninfo_to_dict(dsn)
    database = values.get('dbname')
    if not database:
        raise RuntimeError('Integration DSNs must explicitly name a database.')
    return (values.get('host', ''), values.get('port', '5432'), database)


def disposable_database_pair():
    if os.environ.get('SKELETON_TEST_ALLOW_SCHEMA_RESET') != '1':
        raise RuntimeError('Set SKELETON_TEST_ALLOW_SCHEMA_RESET=1 for a disposable integration database.')
    runtime = os.environ.get('TEST_DATABASE_URL')
    owner = os.environ.get('TEST_MIGRATION_DATABASE_URL')
    if not runtime or not owner:
        raise RuntimeError('TEST_DATABASE_URL and TEST_MIGRATION_DATABASE_URL are both required.')
    identity = _identity(runtime)
    if identity != _identity(owner):
        raise RuntimeError('Runtime and migration DSNs must target the same host, port and database.')
    if not identity[2].endswith('_skeleton_test'):
        raise RuntimeError('Integration database name must end with _skeleton_test.')
    for name in ('DATABASE_URL', 'MIGRATION_DATABASE_URL'):
        configured = os.environ.get(name)
        if configured and _identity(configured) == identity:
            raise RuntimeError(f'Integration database must differ from {name}.')
    if conninfo_to_dict(runtime).get('user') != 'skeleton_runtime':
        raise RuntimeError('Assertions require the skeleton_runtime role, not an owner or superuser.')
    if conninfo_to_dict(owner).get('user') != 'skeleton_owner':
        raise RuntimeError('Schema setup requires the dedicated skeleton_owner role.')
    return DatabasePair(runtime, owner, identity[2])


def reset_schema(pair):
    """Do not create a database/role or touch anything outside schema skeleton."""
    import psycopg
    with psycopg.connect(pair.owner, autocommit=True) as connection:
        identity = connection.execute('SELECT current_database(), current_user').fetchone()
        if identity != (pair.database, 'skeleton_owner'):
            raise RuntimeError('Connected migration identity differs from the validated DSN.')
        role = connection.execute('SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
        if any(role):
            raise RuntimeError('Integration migrator must not be a PostgreSQL superuser/role/database creator.')
        connection.execute('DROP SCHEMA IF EXISTS skeleton CASCADE')
        connection.execute('CREATE SCHEMA skeleton AUTHORIZATION skeleton_owner')
        connection.execute('REVOKE ALL ON SCHEMA skeleton FROM PUBLIC')
        connection.execute('GRANT USAGE ON SCHEMA skeleton TO skeleton_runtime, skeleton_audit_writer')
