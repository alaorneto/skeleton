import uuid
import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
from django.db import migrations, models
from skeleton.common.migration_operations import InstallRecordInfrastructure


class Migration(migrations.Migration):
    dependencies = [('sk_system', '0002_technical_privileges'), ('sk_audit', '0002_record_infrastructure')]
    operations = [
        migrations.CreateModel(
            name='AppInstallation',
            fields=[
                ('sysid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('updated_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('created_by', models.UUIDField(blank=True, editable=False, null=True)),
                ('updated_by', models.UUIDField(blank=True, editable=False, null=True)),
                ('deleted_by', models.UUIDField(blank=True, editable=False, null=True)),
                ('deleted_at', models.DateTimeField(blank=True, editable=False, null=True)),
                ('revision', models.PositiveBigIntegerField(default=1, editable=False)),
                ('app_label', models.CharField(max_length=100)),
                ('enabled', models.BooleanField(default=True)),
                ('configuration', models.JSONField(blank=True, default=dict)),
                ('organization', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='%(app_label)s_%(class)s_records', to='sk_tenancy.organization')),
            ],
            options={
                'abstract': False, 'base_manager_name': 'all_objects', 'default_manager_name': 'objects',
                'constraints': [
                    models.CheckConstraint(condition=models.Q(revision__gte=1), name='sk_system_appinstallation_revision_positive'),
                    models.CheckConstraint(condition=models.Q(deleted_by__isnull=True) | models.Q(deleted_at__isnull=False), name='sk_system_appinstallation_deleted_actor'),
                    models.UniqueConstraint(fields=('organization', 'sysid'), name='sk_system_appinstallation_org_sysid', violation_error_code='unique'),
                    models.UniqueConstraint(fields=('organization', 'app_label'), name='sk_system_appinstallation_unique'),
                ],
            },
            managers=[('objects', django.db.models.manager.Manager()), ('all_objects', django.db.models.manager.Manager())],
        ),
        InstallRecordInfrastructure('sk_system', 'appinstallation', {
            'scope': 'organization', 'immutable_fields': ['app_label'], 'sensitive_fields': ['configuration'],
            'columns': [
                {'name': 'sysid', 'type': 'UUIDField', 'sensitive': False},
                {'name': 'created_at', 'type': 'DateTimeField', 'sensitive': False},
                {'name': 'updated_at', 'type': 'DateTimeField', 'sensitive': False},
                {'name': 'created_by', 'type': 'UUIDField', 'sensitive': False},
                {'name': 'updated_by', 'type': 'UUIDField', 'sensitive': False},
                {'name': 'deleted_by', 'type': 'UUIDField', 'sensitive': False},
                {'name': 'deleted_at', 'type': 'DateTimeField', 'sensitive': False},
                {'name': 'revision', 'type': 'PositiveBigIntegerField', 'sensitive': False},
                {'name': 'organization_id', 'type': 'UUIDField', 'sensitive': False},
                {'name': 'app_label', 'type': 'CharField', 'sensitive': False},
                {'name': 'enabled', 'type': 'BooleanField', 'sensitive': False},
                {'name': 'configuration', 'type': 'JSONField', 'sensitive': True},
            ],
        }),
    ]
