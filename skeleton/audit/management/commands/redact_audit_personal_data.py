"""Redact the personal data already written in the audit (#189): the one exception to append-only.

New events never carry ``personal_fields`` (the triggers redact them). Events written before
the declaration still do, so this command rewrites them once: for every table with
``personal_fields``, the ``old``/``new`` values of those columns in ``changes`` become
``[REDACTED]`` and ``redacted`` becomes true. Nothing else in the event changes.

It runs only as the owner of the audit table (``MIGRATION_DATABASE_URL``, or ``--database-url``):
inside one transaction it disables the audit guard trigger, rewrites, enables it again and
commits, so the table is never left writable. The runtime role is refused. It needs
``--confirm`` and records a ``audit.personal_data_redacted`` security event with the count.
Take a backup first: the rewrite cannot be undone.
"""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError

AUDIT_TABLE = "sk_audit_auditevent"


def _redaction(column: str) -> str:
    value = "CASE WHEN changes->%(c)s->'{side}' IS NULL OR changes->%(c)s->'{side}' = 'null'::jsonb THEN 'null'::jsonb ELSE to_jsonb('[REDACTED]'::text) END"
    return (
        "jsonb_set(changes, ARRAY[%(c)s], jsonb_build_object('old', "
        + value.format(side="old")
        + ", 'new', "
        + value.format(side="new")
        + ", 'redacted', true))"
    )


class Command(BaseCommand):
    help = "Redact personal_fields in audit events written before their declaration (owner only)."

    def add_arguments(self, parser):
        parser.add_argument("--database-url", default=None, help="Owner URL; defaults to MIGRATION_DATABASE_URL.")
        parser.add_argument("--confirm", action="store_true", help="Required: the rewrite cannot be undone.")

    def handle(self, *args, database_url=None, confirm=False, **options):
        import psycopg
        from psycopg import sql

        from skeleton.common.logging import security_event
        from skeleton.system.registry import all_specs

        if not confirm:
            raise CommandError("Refusing to rewrite the audit without --confirm (take a backup first).")
        url = database_url or os.environ.get("MIGRATION_DATABASE_URL")
        if not url:
            raise CommandError("Set MIGRATION_DATABASE_URL (owner) or pass --database-url.")
        targets = {
            spec.model._meta.db_table: [spec.model._meta.get_field(name).column for name in spec.personal_fields]
            for spec in all_specs()
            if getattr(spec, "personal_fields", ())
        }
        total = 0
        with psycopg.connect(url) as connection:
            owner = connection.execute(
                "SELECT tableowner = current_user FROM pg_tables WHERE schemaname = 'skeleton' AND tablename = %s",
                [AUDIT_TABLE],
            ).fetchone()
            if not owner or not owner[0]:
                raise CommandError("Only the owner of skeleton.sk_audit_auditevent may redact the audit.")
            trigger = sql.Identifier(AUDIT_TABLE + "_sk_guard")
            table = sql.SQL("skeleton.") + sql.Identifier(AUDIT_TABLE)
            with connection.transaction():
                connection.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(table, trigger))
                for table_name, columns in sorted(targets.items()):
                    for column in columns:
                        statement = sql.SQL(
                            "UPDATE {table} SET changes = "
                            + _redaction(column).replace("%(c)s", "%(c)s")
                            + " WHERE table_name = %(t)s AND changes ? %(c)s"
                            + " AND COALESCE((changes->%(c)s->>'redacted')::boolean, false) = false"
                        ).format(table=table)
                        cursor = connection.execute(statement, {"c": column, "t": table_name})
                        total += cursor.rowcount
                connection.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(table, trigger))
        security_event("audit.personal_data_redacted", count=total, outcome="success")
        self.stdout.write(f"{total} audit value(s) redacted.")
