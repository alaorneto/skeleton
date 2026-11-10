# Service accounts (#44): account type, the single owning organization and the reinstalled
# record infrastructure of User, so the audit trigger and immutability cover the new columns.

import django.db.models.deletion
from django.db import migrations, models

from skeleton.common.migration_operations import InstallRecordInfrastructure

BASE_COLUMNS = [
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
    {"name": "email", "type": "EmailField", "sensitive": False},
    {"name": "first_name", "type": "CharField", "sensitive": False},
    {"name": "last_name", "type": "CharField", "sensitive": False},
    {"name": "is_active", "type": "BooleanField", "sensitive": False},
    {"name": "is_staff", "type": "BooleanField", "sensitive": False},
    {"name": "auth_version", "type": "PositiveBigIntegerField", "sensitive": False},
    {"name": "local_password_allowed", "type": "BooleanField", "sensitive": False},
]
PREVIOUS = {
    "scope": "global",
    "append_only": False,
    "sensitive_fields": ["password"],
    "immutable_fields": [],
    "columns": BASE_COLUMNS,
}
CURRENT = {
    "scope": "global",
    "append_only": False,
    "sensitive_fields": ["password"],
    "immutable_fields": ["account_type", "service_organization_id"],
    "columns": BASE_COLUMNS
    + [
        {"name": "account_type", "type": "CharField", "sensitive": False},
        {"name": "service_organization_id", "type": "UUIDField", "sensitive": False},
    ],
}


class Migration(migrations.Migration):
    dependencies = [
        ("sk_accounts", "0001_initial"),
        ("sk_tenancy", "0003_membership_self_discovery"),
        ("sk_audit", "0002_record_infrastructure"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="account_type",
            field=models.CharField(
                choices=[("human", "Pessoa"), ("service", "Conta de serviço")], default="human", max_length=16
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="service_organization",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="service_accounts",
                to="sk_tenancy.organization",
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("account_type", "human"), ("service_organization__isnull", True)),
                    models.Q(
                        ("account_type", "service"),
                        ("is_staff", False),
                        ("is_superuser", False),
                        ("local_password_allowed", False),
                        ("service_organization__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="sk_accounts_user_service_account",
            ),
        ),
        InstallRecordInfrastructure("sk_accounts", "user", CURRENT, replaces=PREVIOUS),
    ]
