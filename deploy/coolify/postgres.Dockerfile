# PostgreSQL of the Coolify sandbox (#369): the pinned image of compose.yaml plus the reviewed
# bootstrap, applied by the entrypoint only when the data volume is created.
FROM postgres:16.15
COPY deploy/postgres/bootstrap.sql /opt/skeleton/bootstrap.sql
# Executable, so the entrypoint runs it in its own shell instead of sourcing it.
COPY --chmod=0755 deploy/coolify/postgres-init.sh /docker-entrypoint-initdb.d/10-skeleton.sh
