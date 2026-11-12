"""Global audit events become readable by the platform auditor (#34).

Reinstalls the audit table's infrastructure with the same frozen specification: the SELECT
policy now also admits ``skeleton.platform_auditor='on'``, a GUC set only by the platform
audit page after checking the ``audit.read`` capability. INSERT stays with the audit writer;
UPDATE and DELETE remain impossible (append-only).
"""

from django.db import migrations

from skeleton.common.migration_operations import InstallRecordInfrastructure

BASE_COLUMNS = [
    ("sysid", "UUIDField"),
    ("created_at", "DateTimeField"),
    ("updated_at", "DateTimeField"),
    ("created_by", "UUIDField"),
    ("updated_by", "UUIDField"),
    ("deleted_by", "UUIDField"),
    ("deleted_at", "DateTimeField"),
    ("revision", "PositiveBigIntegerField"),
]
COLUMNS = BASE_COLUMNS + [
    ("organization_id", "UUIDField"),
    ("table_name", "CharField"),
    ("model_label", "CharField"),
    ("record_sysid", "UUIDField"),
    ("record_revision", "PositiveBigIntegerField"),
    ("operation", "CharField"),
    ("actor_sysid", "UUIDField"),
    ("request_id", "UUIDField"),
    ("source", "CharField"),
    ("db_principal", "CharField"),
    ("changes", "JSONField"),
]
SPECIFICATION = {
    "scope": "global",
    "append_only": True,
    "sensitive_fields": [],
    "immutable_fields": [],
    "columns": [{"name": column, "type": kind, "sensitive": False} for column, kind in COLUMNS],
}


class Migration(migrations.Migration):
    dependencies = [("sk_audit", "0002_record_infrastructure"), ("sk_accounts", "0005_platform_roles")]
    operations = [InstallRecordInfrastructure("sk_audit", "AuditEvent", SPECIFICATION, replaces=SPECIFICATION)]
