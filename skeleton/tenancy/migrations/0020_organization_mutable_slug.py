# The organization slug may change through skeleton.tenancy.slugs (#765): the guard stops freezing it.

from django.db import migrations

from skeleton.common.migration_operations import InstallRecordInfrastructure

COLUMNS = [
    {"name": name, "type": kind, "sensitive": False}
    for name, kind in (
        ("sysid", "UUIDField"),
        ("created_at", "DateTimeField"),
        ("updated_at", "DateTimeField"),
        ("created_by", "UUIDField"),
        ("updated_by", "UUIDField"),
        ("deleted_by", "UUIDField"),
        ("deleted_at", "DateTimeField"),
        ("revision", "PositiveBigIntegerField"),
        ("name", "CharField"),
        ("slug", "SlugField"),
        ("is_active", "BooleanField"),
    )
]
# As frozen by sk_audit.0002_record_infrastructure.
ORGANIZATION_BEFORE = {
    "scope": "global",
    "append_only": False,
    "sensitive_fields": [],
    "immutable_fields": ["slug"],
    "columns": COLUMNS,
}
ORGANIZATION = {"scope": "global", "sensitive_fields": [], "columns": COLUMNS}


class Migration(migrations.Migration):
    dependencies = [
        ("sk_tenancy", "0019_organization_mfa_policy"),
        ("sk_audit", "0002_record_infrastructure"),
    ]

    operations = [
        InstallRecordInfrastructure("sk_tenancy", "Organization", ORGANIZATION, replaces=ORGANIZATION_BEFORE),
    ]
