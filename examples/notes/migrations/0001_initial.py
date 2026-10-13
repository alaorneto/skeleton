import uuid

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from skeleton.common.migration_operations import InstallRecordInfrastructure


class Migration(migrations.Migration):
    initial = True
    dependencies = [("sk_audit", "0002_record_infrastructure")]
    operations = [
        migrations.CreateModel(
            name="Note",
            fields=[
                ("sysid", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("created_by", models.UUIDField(blank=True, editable=False, null=True)),
                ("updated_by", models.UUIDField(blank=True, editable=False, null=True)),
                ("deleted_by", models.UUIDField(blank=True, editable=False, null=True)),
                ("deleted_at", models.DateTimeField(blank=True, editable=False, null=True)),
                ("revision", models.PositiveBigIntegerField(default=1, editable=False)),
                ("visibility", models.CharField(choices=[("private", "Privado"), ("group", "Grupo"), ("organization", "Organização")], default="private", max_length=16)),
                ("code", models.CharField(max_length=64)),
                ("title", models.CharField(max_length=200)),
                ("body", models.TextField(blank=True, validators=[django.core.validators.MaxLengthValidator(10000)])),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="%(app_label)s_%(class)s_records", to="sk_tenancy.organization")),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="%(app_label)s_%(class)s_owned", to=settings.AUTH_USER_MODEL)),
                ("owning_group", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="%(app_label)s_%(class)s_owned", to="sk_access.accessgroup")),
            ],
            options={
                "ordering": ("created_at", "sysid"),
                "abstract": False,
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "constraints": [
                    models.CheckConstraint(condition=models.Q(revision__gte=1), name="example_notes_note_revision_positive"),
                    models.CheckConstraint(condition=models.Q(deleted_by__isnull=True) | models.Q(deleted_at__isnull=False), name="example_notes_note_deleted_actor"),
                    models.UniqueConstraint(fields=("organization", "sysid"), name="example_notes_note_org_sysid", violation_error_code='unique'),
                    models.CheckConstraint(condition=(models.Q(visibility="group", owning_group__isnull=False) | models.Q(visibility__in=("private", "organization"), owning_group__isnull=True)), name="example_notes_note_visibility_group"),
                    models.UniqueConstraint(fields=("organization", "code"), condition=models.Q(deleted_at__isnull=True), name="example_notes_note_live_code", violation_error_code='unique'),
                ],
            },
            managers=[("objects", models.Manager()), ("all_objects", models.Manager())],
        ),
        InstallRecordInfrastructure(
            "example_notes",
            "note",
            {
                "scope": "organization",
                "sensitive_fields": ["body"],
                "columns": [
                    {"name": "sysid", "type": "UUIDField", "sensitive": False},
                    {"name": "created_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "updated_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "created_by", "type": "UUIDField", "sensitive": False},
                    {"name": "updated_by", "type": "UUIDField", "sensitive": False},
                    {"name": "deleted_by", "type": "UUIDField", "sensitive": False},
                    {"name": "deleted_at", "type": "DateTimeField", "sensitive": False},
                    {"name": "revision", "type": "PositiveBigIntegerField", "sensitive": False},
                    {"name": "organization_id", "type": "UUIDField", "sensitive": False},
                    {"name": "owner_id", "type": "UUIDField", "sensitive": False},
                    {"name": "visibility", "type": "CharField", "sensitive": False},
                    {"name": "owning_group_id", "type": "UUIDField", "sensitive": False},
                    {"name": "code", "type": "CharField", "sensitive": False},
                    {"name": "title", "type": "CharField", "sensitive": False},
                    {"name": "body", "type": "TextField", "sensitive": True},
                ],
            },
        ),
    ]
