import uuid

import django.db.models.manager
import django.utils.timezone
from django.db import migrations, models

from skeleton.common.migration_operations import InstallRecordInfrastructure


def record_fields():
    return [
        ('sysid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
        ('updated_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
        ('created_by', models.UUIDField(blank=True, editable=False, null=True)),
        ('updated_by', models.UUIDField(blank=True, editable=False, null=True)),
        ('deleted_by', models.UUIDField(blank=True, editable=False, null=True)),
        ('deleted_at', models.DateTimeField(blank=True, editable=False, null=True)),
        ('revision', models.PositiveBigIntegerField(default=1, editable=False)),
    ]


def record_options(name):
    return {
        'abstract': False, 'base_manager_name': 'all_objects', 'default_manager_name': 'objects',
        'constraints': [
            models.CheckConstraint(condition=models.Q(revision__gte=1), name=f'sk_system_{name}_revision_positive'),
            models.CheckConstraint(condition=models.Q(deleted_by__isnull=True) | models.Q(deleted_at__isnull=False), name=f'sk_system_{name}_deleted_actor'),
        ],
    }


def specification(fields, immutable, sensitive=()):
    return {
        'scope': 'global', 'immutable_fields': list(immutable), 'sensitive_fields': list(sensitive),
        'columns': [{'name': name, 'type': field.get_internal_type(), 'sensitive': name in sensitive} for name, field in fields],
    }


COMPONENT_FIELDS = record_fields() + [
    ('component_id', models.CharField(max_length=100, unique=True)),
    ('kind', models.CharField(choices=[('core', 'Core'), ('app', 'App')], max_length=8)),
    ('app_label', models.CharField(blank=True, max_length=100)),
    ('package_name', models.CharField(blank=True, max_length=200)),
    ('version', models.CharField(max_length=64)),
    ('digest', models.CharField(blank=True, max_length=64)),
    ('publisher', models.CharField(blank=True, max_length=100)),
    ('generation', models.PositiveBigIntegerField(default=0)),
    ('state', models.CharField(choices=[('installed', 'Instalado'), ('project-local', 'Local'), ('schema_retained', 'Dados preservados')], max_length=24)),
    ('inventory_reference', models.CharField(blank=True, max_length=200)),
]
OPERATION_FIELDS = record_fields() + [
    ('operation', models.CharField(choices=[('install', 'Instalação'), ('update', 'Atualização'), ('uninstall', 'Desinstalação')], max_length=16)),
    ('state', models.CharField(default='preview', max_length=24)),
    ('stage', models.CharField(default='preview', max_length=64)),
    ('expected_generation', models.PositiveBigIntegerField()),
    ('plan_digest', models.CharField(max_length=64)),
    ('plan', models.JSONField(default=dict)),
    ('result', models.JSONField(blank=True, default=dict)),
    ('error_code', models.CharField(blank=True, max_length=100)),
]


class Migration(migrations.Migration):
    dependencies = [('sk_system', '0003_appinstallation')]
    operations = [
        migrations.CreateModel(
            name='ComponentInstallation', fields=COMPONENT_FIELDS,
            options=record_options('componentinstallation'),
            managers=[('objects', django.db.models.manager.Manager()), ('all_objects', django.db.models.manager.Manager())],
        ),
        InstallRecordInfrastructure('sk_system', 'componentinstallation', specification(
            COMPONENT_FIELDS, ('component_id', 'kind', 'app_label', 'package_name'),
        )),
        migrations.CreateModel(
            name='DeploymentOperation', fields=OPERATION_FIELDS,
            options=record_options('deploymentoperation'),
            managers=[('objects', django.db.models.manager.Manager()), ('all_objects', django.db.models.manager.Manager())],
        ),
        InstallRecordInfrastructure('sk_system', 'deploymentoperation', specification(
            OPERATION_FIELDS, ('operation', 'expected_generation', 'plan_digest', 'plan'), ('plan', 'result'),
        )),
    ]
