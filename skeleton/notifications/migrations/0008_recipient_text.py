from django.db import migrations, models

from skeleton.common.migration_operations import InstallRecordInfrastructure


class Migration(migrations.Migration):
    dependencies = [
        ("sk_audit", "0002_record_infrastructure"),
        ("sk_notifications", "0007_inbox_updated_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationrecipient",
            name="title",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
        migrations.AddField(
            model_name="notificationrecipient",
            name="body",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="notificationrecipient",
            name="subject_people",
            field=models.JSONField(default=list),
        ),
        InstallRecordInfrastructure(
            "sk_notifications",
            "notificationrecipient",
            {
                "scope": "organization",
                "sensitive_fields": [],
                "lifecycle": "operational",
                "self_discovery_column": "user_id",
                "immutable_fields": ["user_id"],
                "columns": [
                    {"name": "sysid", "type": "UUIDField", "sensitive": False},
                    {"name": "created_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "updated_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "organization_id", "type": "UUIDField", "sensitive": False},
                    {"name": "notification_id", "type": "UUIDField", "sensitive": False},
                    {"name": "user_id", "type": "UUIDField", "sensitive": False},
                    {"name": "type_code", "type": "CharField", "sensitive": False},
                    {"name": "target_table_key", "type": "CharField", "sensitive": False},
                    {"name": "target_sysid", "type": "UUIDField", "sensitive": False},
                    {"name": "snapshot", "type": "JSONField", "sensitive": False},
                    {"name": "payload", "type": "JSONField", "sensitive": False},
                    {"name": "group_key", "type": "CharField", "sensitive": False},
                    {"name": "count", "type": "PositiveIntegerField", "sensitive": False},
                    {"name": "read_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "archived_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "digest_sent_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "title", "type": "CharField", "sensitive": False},
                    {"name": "body", "type": "TextField", "sensitive": False},
                    {"name": "subject_people", "type": "JSONField", "sensitive": False},
                ],
            },
            replaces={
                "scope": "organization",
                "sensitive_fields": [],
                "lifecycle": "operational",
                "self_discovery_column": "user_id",
                "immutable_fields": ["user_id"],
                "columns": [
                    {"name": "sysid", "type": "UUIDField", "sensitive": False},
                    {"name": "created_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "updated_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "organization_id", "type": "UUIDField", "sensitive": False},
                    {"name": "notification_id", "type": "UUIDField", "sensitive": False},
                    {"name": "user_id", "type": "UUIDField", "sensitive": False},
                    {"name": "type_code", "type": "CharField", "sensitive": False},
                    {"name": "target_table_key", "type": "CharField", "sensitive": False},
                    {"name": "target_sysid", "type": "UUIDField", "sensitive": False},
                    {"name": "snapshot", "type": "JSONField", "sensitive": False},
                    {"name": "payload", "type": "JSONField", "sensitive": False},
                    {"name": "group_key", "type": "CharField", "sensitive": False},
                    {"name": "count", "type": "PositiveIntegerField", "sensitive": False},
                    {"name": "read_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "archived_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "digest_sent_at", "type": "DateTimeField", "sensitive": False},
                ],
            },
        ),
    ]
