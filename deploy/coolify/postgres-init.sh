#!/bin/sh
# Sourced by the postgres entrypoint on the first start of an empty volume only (#369).
# Applies the reviewed bootstrap and sets the role passwords from the Coolify variables;
# psql interpolates them as quoted literals (:'name'), so they never reach the command line.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /opt/skeleton/bootstrap.sql

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v owner_password="$SKELETON_OWNER_DB_PASSWORD" \
    -v runtime_password="$SKELETON_RUNTIME_DB_PASSWORD" <<'SQL'
ALTER ROLE skeleton_owner PASSWORD :'owner_password';
ALTER ROLE skeleton_runtime PASSWORD :'runtime_password';
SQL
