"""When the platform last reset the person's MFA (#736).

``mfa_reset_at`` joins the audited columns of the identity, so the audit triggers record each
reset with its actor. It is neither personal nor sensitive. The infrastructure frozen by 0004 is
superseded and restored when the migration is reversed.
"""

from django.db import migrations, models

from skeleton.common.migration_operations import InstallRecordInfrastructure


def user(*, with_mfa_reset: bool) -> dict:
    columns = [
        {"name": "sysid", "type": "UUIDField", "sensitive": False},
        {"name": "created_at", "type": "DateTimeField", "sensitive": False},
        {"name": "updated_at", "type": "DateTimeField", "sensitive": False},
        {"name": "created_by", "type": "UUIDField", "sensitive": False},
        {"name": "updated_by", "type": "UUIDField", "sensitive": False},
        {"name": "deleted_by", "type": "UUIDField", "sensitive": False},
        {"name": "deleted_at", "type": "DateTimeField", "sensitive": False},
        {"name": "revision", "type": "PositiveBigIntegerField", "sensitive": False},
        {"name": "password", "type": "CharField", "sensitive": True},
        {"name": "last_login", "type": "DateTimeField", "sensitive": False},
        {"name": "is_superuser", "type": "BooleanField", "sensitive": False},
        {"name": "email", "type": "EmailField", "sensitive": False, "personal": True},
        {"name": "first_name", "type": "CharField", "sensitive": False, "personal": True},
        {"name": "last_name", "type": "CharField", "sensitive": False, "personal": True},
        {"name": "is_active", "type": "BooleanField", "sensitive": False},
        {"name": "is_staff", "type": "BooleanField", "sensitive": False},
        {"name": "auth_version", "type": "PositiveBigIntegerField", "sensitive": False},
        {"name": "local_password_allowed", "type": "BooleanField", "sensitive": False},
    ]
    if with_mfa_reset:
        columns.append({"name": "mfa_reset_at", "type": "DateTimeField", "sensitive": False})
    columns += [
        {"name": "account_type", "type": "CharField", "sensitive": False},
        {"name": "service_organization_id", "type": "UUIDField", "sensitive": False},
    ]
    return {
        "scope": "global",
        "append_only": False,
        "sensitive_fields": ["password"],
        "immutable_fields": ["account_type", "service_organization_id"],
        "columns": columns,
        "personal_fields": ["email", "first_name", "last_name"],
    }


class Migration(migrations.Migration):
    dependencies = [("sk_accounts", "0012_platform_clients"), ("sk_audit", "0003_investigation_indexes")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="mfa_reset_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        InstallRecordInfrastructure(
            "sk_accounts",
            "user",
            user(with_mfa_reset=True),
            replaces=user(with_mfa_reset=False),
        ),
    ]
