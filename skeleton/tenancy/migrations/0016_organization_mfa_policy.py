# The organization's MFA policy (#740).

from django.db import migrations, models

from skeleton.common.migration_operations import InstallRecordInfrastructure

BASE_COLUMNS = (
    ("sysid", "UUIDField"),
    ("created_at", "DateTimeField"),
    ("updated_at", "DateTimeField"),
    ("created_by", "UUIDField"),
    ("updated_by", "UUIDField"),
    ("deleted_by", "UUIDField"),
    ("deleted_at", "DateTimeField"),
    ("revision", "PositiveBigIntegerField"),
)
SENSITIVE = {"logo_key", "icon_key"}


def columns(*extra):
    return [{"name": name, "type": kind, "sensitive": name in SENSITIVE} for name, kind in (*BASE_COLUMNS, *extra)]


SETTINGS_COLUMNS = (
    ("organization_id", "UUIDField"),
    ("display_name", "CharField"),
    ("default_language", "CharField"),
    ("default_timezone", "CharField"),
    ("brand_color", "CharField"),
    ("logo_key", "CharField"),
    ("business_hours", "JSONField"),
    ("holidays", "JSONField"),
    ("site_enabled", "BooleanField"),
    ("icon_key", "CharField"),
)
SETTINGS_BEFORE = {
    "scope": "organization",
    "sensitive_fields": ["logo_key", "icon_key"],
    "columns": columns(*SETTINGS_COLUMNS),
}
SETTINGS = {
    "scope": "organization",
    "sensitive_fields": ["logo_key", "icon_key"],
    "columns": columns(*SETTINGS_COLUMNS, ("mfa_policy", "CharField")),
}


class Migration(migrations.Migration):

    dependencies = [
        ("sk_tenancy", "0015_organization_pages"),
    ]

    operations = [
        migrations.AddField(
            model_name="organizationsettings",
            name="mfa_policy",
            field=models.CharField(
                blank=True,
                choices=[("", "Segue a plataforma"), ("admins", "Quem administra a organização"), ("all", "Todos os membros")],
                default="",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="organizationsettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("mfa_policy__in", ("", "admins", "all"))), name="sk_tenancy_settings_mfa_policy"
            ),
        ),
        InstallRecordInfrastructure("sk_tenancy", "organizationsettings", SETTINGS, replaces=SETTINGS_BEFORE),
    ]
