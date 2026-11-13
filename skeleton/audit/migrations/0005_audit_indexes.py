"""Audit indexes match the queries that read them (#270).

The history of a record and the record filter of the log read ``record_sysid`` newest first,
so ``(record_sysid, created_at)`` replaces ``(table_name, record_sysid, record_revision)``,
which no query used (none filters by ``table_name``). The single-column indexes on
``organization_id`` and ``record_sysid`` go away: the first is a prefix of
``sk_audit_org_created`` and ``sk_audit_org_actor``, the second a prefix of the new index.

CREATE/DROP INDEX CONCURRENTLY cannot run inside a transaction. AuditEvent receives a row for
every write of the system, so a plain CREATE INDEX would block all writes while it builds.
The new index is created before the old ones are dropped, so the history is never left
without one. An interrupted run is recovered by the owner with
``DROP INDEX CONCURRENTLY IF EXISTS skeleton.sk_audit_record_created;`` and ``migrate`` again.
"""

from django.contrib.postgres.operations import AddIndexConcurrently, RemoveIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [("sk_audit", "0004_platform_auditor")]

    operations = [
        AddIndexConcurrently(
            "auditevent",
            models.Index(fields=["record_sysid", "created_at"], name="sk_audit_record_created"),
        ),
        RemoveIndexConcurrently("auditevent", "sk_audit_record_revision"),
        migrations.AlterField("auditevent", "record_sysid", models.UUIDField()),
        migrations.AlterField("auditevent", "organization_id", models.UUIDField(blank=True, null=True)),
    ]
