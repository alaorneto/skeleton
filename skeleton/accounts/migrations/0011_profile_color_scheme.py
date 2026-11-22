"""The colour scheme of the person's interface (#666): light, dark or the system's.

``color_scheme`` joins the audited columns of the profile: it is a preference like the language,
neither personal nor sensitive. The infrastructure frozen by 0004 is superseded and restored
when the migration is reversed.
"""

from django.db import migrations, models

from skeleton.common.migration_operations import InstallRecordInfrastructure


def profile(*, with_color_scheme: bool) -> dict:
    columns = [
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
    ]
    if with_color_scheme:
        columns.append({"name": "color_scheme", "type": "CharField", "sensitive": False})
    return {
        "scope": "global",
        "append_only": False,
        "sensitive_fields": [],
        "immutable_fields": ["user_id"],
        "columns": columns,
        "personal_fields": ["display_name"],
    }


class Migration(migrations.Migration):
    dependencies = [("sk_accounts", "0010_native_clients"), ("sk_audit", "0003_investigation_indexes")]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="color_scheme",
            field=models.CharField(
                blank=True,
                choices=[("", "Seguir o sistema"), ("light", "Claro"), ("dark", "Escuro")],
                default="",
                max_length=8,
            ),
        ),
        InstallRecordInfrastructure(
            "sk_accounts",
            "userprofile",
            profile(with_color_scheme=True),
            replaces=profile(with_color_scheme=False),
        ),
    ]
