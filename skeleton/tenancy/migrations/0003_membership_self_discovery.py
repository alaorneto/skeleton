from django.db import migrations
from skeleton.common.migration_operations import InstallRecordInfrastructure

COLUMNS = [
    ("sysid", "UUIDField"), ("created_at", "DateTimeField"), ("updated_at", "DateTimeField"),
    ("created_by", "UUIDField"), ("updated_by", "UUIDField"), ("deleted_by", "UUIDField"),
    ("deleted_at", "DateTimeField"), ("revision", "PositiveBigIntegerField"), ("organization_id", "ForeignKey"),
    ("user_id", "ForeignKey"), ("is_active", "BooleanField"),
]


def specification(**extra):
    return {
        "scope": "organization", "append_only": False, "sensitive_fields": [], "immutable_fields": ["user_id"],
        "columns": [{"name": column, "type": kind, "sensitive": False} for column, kind in COLUMNS],
        **extra,
    }


class Migration(migrations.Migration):
    """Membership self-discovery moves from a label special case to the frozen ``self_discovery_column`` key.

    The effective policy is unchanged (SELECT tenant OR own user; writes only tenant); only the
    policy names become ``skeleton_self_*``. Backwards reinstalls the historical specification.
    """

    dependencies = [("sk_tenancy", "0002_invitation"), ("sk_audit", "0002_record_infrastructure")]
    operations = [
        InstallRecordInfrastructure(
            "sk_tenancy", "Membership", specification(self_discovery_column="user_id"), replaces=specification()
        ),
    ]
