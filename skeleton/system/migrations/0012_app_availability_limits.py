# Organization limits added by each app (#696): a platform decision of the instance.

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
AVAILABILITY_COLUMNS = (("app_label", "CharField"), ("mode", "CharField"), ("upgrade_state", "CharField"))


def columns(*extra):
    return [{"name": name, "type": kind, "sensitive": False} for name, kind in (*BASE_COLUMNS, *extra)]


AVAILABILITY_BEFORE = {
    "scope": "global",
    "sensitive_fields": [],
    "immutable_fields": ["app_label"],
    "columns": columns(*AVAILABILITY_COLUMNS),
}
AVAILABILITY = {
    "scope": "global",
    "sensitive_fields": [],
    "immutable_fields": ["app_label"],
    "columns": columns(*AVAILABILITY_COLUMNS, ("limits", "JSONField")),
}


class Migration(migrations.Migration):

    dependencies = [
        ("sk_system", "0011_native_client_releases"),
    ]

    operations = [
        migrations.AddField(
            model_name="appavailability",
            name="limits",
            field=models.JSONField(blank=True, default=dict),
        ),
        InstallRecordInfrastructure("sk_system", "appavailability", AVAILABILITY, replaces=AVAILABILITY_BEFORE),
    ]
