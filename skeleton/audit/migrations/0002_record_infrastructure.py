from django.db import migrations
from skeleton.common.migration_operations import InstallRecordInfrastructure
from skeleton.common.lifecycle_operation import InstallUserLifecycleGuard

BASE_COLUMNS = [
    ("sysid", "UUIDField"), ("created_at", "DateTimeField"), ("updated_at", "DateTimeField"),
    ("created_by", "UUIDField"), ("updated_by", "UUIDField"), ("deleted_by", "UUIDField"),
    ("deleted_at", "DateTimeField"), ("revision", "PositiveBigIntegerField"),
]


def infrastructure(app_label, name, columns, *, tenant=False, sensitive=(), immutable=(), append_only=False):
    columns = BASE_COLUMNS + ([('organization_id', 'ForeignKey')] if tenant else []) + columns
    return InstallRecordInfrastructure(app_label, name, {
        "scope": "organization" if tenant else "global", "append_only": append_only,
        "sensitive_fields": list(sensitive), "immutable_fields": list(immutable),
        "columns": [{"name": column, "type": kind, "sensitive": column in sensitive} for column, kind in columns],
    })


class Migration(migrations.Migration):
    dependencies = [
        ("sk_accounts", "0001_initial"), ("sk_tenancy", "0002_invitation"),
        ("sk_access", "0001_initial"), ("sk_system", "0001_initial"), ("sk_audit", "0001_initial"),
    ]
    operations = [
        infrastructure("sk_audit", "AuditEvent", [
            ("organization_id", "UUIDField"), ("table_name", "CharField"), ("model_label", "CharField"),
            ("record_sysid", "UUIDField"), ("record_revision", "PositiveBigIntegerField"), ("operation", "CharField"),
            ("actor_sysid", "UUIDField"), ("request_id", "UUIDField"), ("source", "CharField"),
            ("db_principal", "CharField"), ("changes", "JSONField"),
        ], append_only=True),
        infrastructure("sk_accounts", "User", [
            ("password", "CharField"), ("last_login", "DateTimeField"), ("is_superuser", "BooleanField"),
            ("email", "EmailField"), ("first_name", "CharField"), ("last_name", "CharField"),
            ("is_active", "BooleanField"), ("is_staff", "BooleanField"), ("auth_version", "PositiveBigIntegerField"),
            ("local_password_allowed", "BooleanField"),
        ], sensitive=("password",)),
        infrastructure("sk_accounts", "UserProfile", [
            ("user_id", "OneToOneField"), ("display_name", "CharField"), ("timezone", "CharField"), ("language", "CharField"),
        ], immutable=("user_id",)),
        infrastructure("sk_tenancy", "Organization", [
            ("name", "CharField"), ("slug", "SlugField"), ("is_active", "BooleanField"),
        ], immutable=("slug",)),
        infrastructure("sk_tenancy", "Membership", [("user_id", "ForeignKey"), ("is_active", "BooleanField")], tenant=True, immutable=("user_id",)),
        infrastructure("sk_system", "TableDefinition", [
            ("database_alias", "CharField"), ("schema_name", "CharField"), ("db_table", "CharField"),
            ("model_label", "CharField"), ("label", "CharField"), ("description", "TextField"),
            ("origin", "CharField"), ("scope", "CharField"), ("base_class", "CharField"),
            ("uuid_managed", "BooleanField"), ("soft_delete", "BooleanField"), ("audit_mode", "CharField"),
            ("api_exposed", "BooleanField"), ("available", "BooleanField"),
        ]),
        infrastructure("sk_system", "FieldDefinition", [
            ("table_id", "ForeignKey"), ("name", "CharField"), ("column", "CharField"), ("label", "CharField"),
            ("description", "TextField"), ("type_name", "CharField"), ("nullable", "BooleanField"),
            ("primary_key", "BooleanField"), ("relation_target", "CharField"), ("sensitive", "BooleanField"), ("available", "BooleanField"),
        ]),
        infrastructure("sk_access", "Role", [("code", "SlugField"), ("name", "CharField"), ("description", "TextField"), ("is_builtin", "BooleanField")], tenant=True),
        infrastructure("sk_access", "AccessGroup", [("code", "SlugField"), ("name", "CharField"), ("description", "TextField")], tenant=True),
        infrastructure("sk_access", "MemberRole", [("membership_id", "ForeignKey"), ("role_id", "ForeignKey")], tenant=True, immutable=("membership_id", "role_id")),
        infrastructure("sk_access", "GroupMember", [("group_id", "ForeignKey"), ("membership_id", "ForeignKey")], tenant=True, immutable=("group_id", "membership_id")),
        infrastructure("sk_access", "GroupRole", [("group_id", "ForeignKey"), ("role_id", "ForeignKey")], tenant=True, immutable=("group_id", "role_id")),
        infrastructure("sk_access", "TablePermission", [("role_id", "ForeignKey"), ("table_id", "ForeignKey"), ("action", "CharField")], tenant=True, immutable=("role_id", "table_id", "action")),
        infrastructure("sk_access", "RecordGrant", [("table_id", "ForeignKey"), ("record_sysid", "UUIDField"), ("membership_id", "ForeignKey"), ("group_id", "ForeignKey"), ("role_id", "ForeignKey"), ("action", "CharField")], tenant=True, immutable=("table_id", "record_sysid", "membership_id", "group_id", "role_id", "action")),
        infrastructure("sk_tenancy", "Invitation", [("email", "EmailField"), ("role_id", "ForeignKey"), ("token_digest", "CharField"), ("expires_at", "DateTimeField"), ("accepted_at", "DateTimeField"), ("accepted_by", "UUIDField")], tenant=True, sensitive=("token_digest",), immutable=("email", "role_id", "token_digest", "expires_at")),
        InstallUserLifecycleGuard(),
    ]
