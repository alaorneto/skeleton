"""Redact personal data in the audit of users and profiles (#189).

``personal_fields`` stay readable through the API but the audit triggers write
``[REDACTED]`` for them, so the audit names the person only by sysid. Each operation
supersedes the infrastructure frozen before and restores it when reversed.
"""

from django.db import migrations

from skeleton.common.migration_operations import InstallRecordInfrastructure

USER_PREVIOUS = {
    "scope": "global",
    "append_only": False,
    "sensitive_fields": ["password"],
    "immutable_fields": ["account_type", "service_organization_id"],
    "columns": [
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
        {"name": "account_type", "type": "CharField", "sensitive": False},
        {"name": "service_organization_id", "type": "UUIDField", "sensitive": False},
    ],
}

USER_CURRENT = {
    "scope": "global",
    "append_only": False,
    "sensitive_fields": ["password"],
    "immutable_fields": ["account_type", "service_organization_id"],
    "columns": [
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
        {"name": "account_type", "type": "CharField", "sensitive": False},
        {"name": "service_organization_id", "type": "UUIDField", "sensitive": False},
    ],
    "personal_fields": ["email", "first_name", "last_name"],
}

PROFILE_PREVIOUS = {
    "scope": "global",
    "append_only": False,
    "sensitive_fields": [],
    "immutable_fields": ["user_id"],
    "columns": [
        {"name": "sysid", "type": "UUIDField", "sensitive": False},
        {"name": "created_at", "type": "DateTimeField", "sensitive": False},
        {"name": "updated_at", "type": "DateTimeField", "sensitive": False},
        {"name": "created_by", "type": "UUIDField", "sensitive": False},
        {"name": "updated_by", "type": "UUIDField", "sensitive": False},
        {"name": "deleted_by", "type": "UUIDField", "sensitive": False},
        {"name": "deleted_at", "type": "DateTimeField", "sensitive": False},
        {"name": "revision", "type": "PositiveBigIntegerField", "sensitive": False},
        {"name": "user_id", "type": "OneToOneField", "sensitive": False},
        {"name": "display_name", "type": "CharField", "sensitive": False},
        {"name": "timezone", "type": "CharField", "sensitive": False},
        {"name": "language", "type": "CharField", "sensitive": False},
    ],
}

PROFILE_CURRENT = {
    "scope": "global",
    "append_only": False,
    "sensitive_fields": [],
    "immutable_fields": ["user_id"],
    "columns": [
        {"name": "sysid", "type": "UUIDField", "sensitive": False},
        {"name": "created_at", "type": "DateTimeField", "sensitive": False},
        {"name": "updated_at", "type": "DateTimeField", "sensitive": False},
        {"name": "created_by", "type": "UUIDField", "sensitive": False},
        {"name": "updated_by", "type": "UUIDField", "sensitive": False},
        {"name": "deleted_by", "type": "UUIDField", "sensitive": False},
        {"name": "deleted_at", "type": "DateTimeField", "sensitive": False},
        {"name": "revision", "type": "PositiveBigIntegerField", "sensitive": False},
        {"name": "user_id", "type": "OneToOneField", "sensitive": False},
        {"name": "display_name", "type": "CharField", "sensitive": False, "personal": True},
        {"name": "timezone", "type": "CharField", "sensitive": False},
        {"name": "language", "type": "CharField", "sensitive": False},
    ],
    "personal_fields": ["display_name"],
}


class Migration(migrations.Migration):
    dependencies = [("sk_accounts", "0003_profile_follows_defaults"), ("sk_audit", "0003_investigation_indexes")]

    operations = [
        InstallRecordInfrastructure("sk_accounts", "user", USER_CURRENT, replaces=USER_PREVIOUS),
        InstallRecordInfrastructure("sk_accounts", "userprofile", PROFILE_CURRENT, replaces=PROFILE_PREVIOUS),
    ]
