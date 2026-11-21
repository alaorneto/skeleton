"""Global category catalog of the example app and its frozen infrastructure with system writes (#576)."""

import django.db.models.manager
import skeleton.common.migration_operations
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('example_notes', '0002_search_index'),
        ('sk_audit', '0002_record_infrastructure'),
    ]

    operations = [
        migrations.CreateModel(
            name='NoteCategory',
            fields=[
                ('sysid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('updated_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('created_by', models.UUIDField(blank=True, editable=False, null=True)),
                ('updated_by', models.UUIDField(blank=True, editable=False, null=True)),
                ('deleted_by', models.UUIDField(blank=True, editable=False, null=True)),
                ('deleted_at', models.DateTimeField(blank=True, editable=False, null=True)),
                ('revision', models.PositiveBigIntegerField(default=1, editable=False)),
                ('code', models.CharField(max_length=64)),
                ('title', models.CharField(max_length=200)),
            ],
            options={
                'ordering': ('code',),
                'abstract': False,
                'base_manager_name': 'all_objects',
                'default_manager_name': 'objects',
                'constraints': [models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='example_notes_notecategory_revision_positive'), models.CheckConstraint(condition=models.Q(('deleted_by__isnull', True), ('deleted_at__isnull', False), _connector='OR'), name='example_notes_notecategory_deleted_actor'), models.UniqueConstraint(condition=models.Q(('deleted_at__isnull', True)), fields=('code',), name='example_notes_category_live_code', violation_error_code='unique')],
            },
            managers=[
                ('objects', django.db.models.manager.Manager()),
                ('all_objects', django.db.models.manager.Manager()),
            ],
        ),
        skeleton.common.migration_operations.InstallRecordInfrastructure(
            app_label='example_notes',
            model_name='notecategory',
            specification={'columns': [{'name': 'sysid', 'sensitive': False, 'type': 'UUIDField'}, {'name': 'created_at', 'sensitive': False, 'type': 'DateTimeField'}, {'name': 'updated_at', 'sensitive': False, 'type': 'DateTimeField'}, {'name': 'created_by', 'sensitive': False, 'type': 'UUIDField'}, {'name': 'updated_by', 'sensitive': False, 'type': 'UUIDField'}, {'name': 'deleted_by', 'sensitive': False, 'type': 'UUIDField'}, {'name': 'deleted_at', 'sensitive': False, 'type': 'DateTimeField'}, {'name': 'revision', 'sensitive': False, 'type': 'PositiveBigIntegerField'}, {'name': 'code', 'sensitive': False, 'type': 'CharField'}, {'name': 'title', 'sensitive': False, 'type': 'CharField'}], 'scope': 'global', 'sensitive_fields': [], 'writes': 'system'},
        ),
    ]
