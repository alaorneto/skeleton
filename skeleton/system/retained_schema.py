"""Trusted release snapshots for validating retained tables without importing an app.

This metadata is read only from operator-owned deployment files, never a DB row
or user-supplied path. It supplies schema-check models, not ORM/gateway models.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings

from skeleton.common.migration_operations import InstallRecordInfrastructure
from .registry import TableSpec


class RetainedSchemaError(ValueError):
    pass


_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z_0-9]*\Z")
_FIELD_TYPES = frozenset({
    'UUIDField', 'DateTimeField', 'DateField', 'TimeField', 'DurationField',
    'CharField', 'TextField', 'EmailField', 'SlugField', 'URLField', 'BooleanField',
    'IntegerField', 'BigIntegerField', 'SmallIntegerField', 'PositiveIntegerField',
    'PositiveBigIntegerField', 'PositiveSmallIntegerField', 'FloatField',
    'DecimalField', 'JSONField', 'BinaryField', 'FileField', 'ImageField',
    'GenericIPAddressField', 'ForeignKey', 'OneToOneField',
})


@dataclass
class SchemaField:
    name: str
    column: str
    kind: str
    null: bool
    primary_key: bool
    verbose_name: str
    related_model: object = None
    target_field: object = None

    @property
    def is_relation(self):
        return self.related_model is not None

    def get_internal_type(self):
        return self.kind


class SchemaMeta:
    def __init__(self, entry):
        self.db_table = entry['db_table']
        self.label_lower = entry['model_label']
        self.label = self.label_lower
        self.proxy = False
        self.swapped = False
        self.app_label, self.model_name = self.label_lower.split('.')
        self.verbose_name = entry['label']
        self.concrete_fields = []
        self.fields = self.concrete_fields
        self.pk = None

    def get_field(self, name):
        return next(field for field in self.fields if field.name == name)


def _identifier(value):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) or len(value) > 128:
        raise RetainedSchemaError('Invalid retained schema identifier.')
    return value


def export_schema(app_label):
    """CLI-only: freeze the exact installed contract and aggregate impact."""
    from django.apps import apps
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Organization
    from .checks import _frozen_operations
    from .registry import all_specs
    _identifier(app_label)
    operations, _, _ = _frozen_operations()
    tables = []
    models = []
    for spec in all_specs():
        model = spec.model
        if model._meta.app_label != app_label:
            continue
        operation = operations.get(model._meta.label_lower)
        if operation is None:
            raise RetainedSchemaError('Application infrastructure is not frozen.')
        columns = []
        for field in model._meta.concrete_fields:
            item = {
                'name': field.name, 'column': field.column,
                'type': field.get_internal_type(), 'nullable': field.null,
                'primary_key': field.primary_key, 'label': str(field.verbose_name),
            }
            if field.is_relation:
                target = field.related_model
                item['relation'] = {
                    'model_label': target._meta.label_lower, 'db_table': target._meta.db_table,
                    'organization': any(f.name == 'organization' for f in target._meta.concrete_fields),
                    'target_type': field.target_field.get_internal_type(),
                }
            columns.append(item)
        tables.append({
            'model_label': model._meta.label_lower, 'db_table': model._meta.db_table,
            'label': spec.label or str(model._meta.verbose_name), 'description': spec.description,
            'scope': spec.scope, 'sensitive_fields': list(spec.sensitive_fields),
            'columns': columns, 'infrastructure': operation.specification,
        })
        models.append(model)
    org_ids = list(Organization.all_objects.values_list('sysid', flat=True))
    records, affected = 0, set()
    for model in models:
        if any(f.name == 'organization' for f in model._meta.concrete_fields):
            for org_id in org_ids:
                with system_context(reason='deployment:schema-export', organization_id=org_id):
                    count = model.all_objects.filter(organization_id=org_id).count()
                records += count
                if count:
                    affected.add(str(org_id))
        else:
            records += model.all_objects.count()
    references = []
    owned_tables = {entry['db_table'] for entry in tables}
    for model in apps.get_models():
        if model._meta.db_table in owned_tables:
            continue
        for field in model._meta.concrete_fields:
            if field.is_relation and field.related_model._meta.db_table in owned_tables:
                references.append({'from_table': model._meta.db_table,
                                   'target_table': field.related_model._meta.db_table,
                                   'field': field.column})
    return {'format': 1, 'app_label': app_label, 'tables': tables,
            'impact': {'organizations': len(affected), 'records': records, 'references': references}}


def load_retained_contracts():
    """Return isolated schema specifications and operations; never register them."""
    specs, operations = [], {}
    seen_tables, seen_labels = set(), set()
    for reference in getattr(settings, 'SKELETON_RETAINED_SCHEMA_PATHS', ()):
        # A digest in generated inventory detects corruption/drift of retained state.
        if isinstance(reference, dict):
            path, expected = reference['path'], reference['sha256']
        else:
            path, expected = reference, None
        with Path(path).open('rb') as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise RetainedSchemaError('Retained schema snapshot exceeds the limit.')
        if expected and hashlib.sha256(raw).hexdigest() != expected:
            raise RetainedSchemaError('Retained schema digest mismatch.')
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get('format') != 1 or not isinstance(document.get('tables'), list):
            raise RetainedSchemaError('Unsupported retained schema snapshot.')
        app_label = _identifier(document['app_label'])
        for entry in document['tables']:
            label = entry['model_label']
            if not isinstance(label, str) or len(label.split('.')) != 2:
                raise RetainedSchemaError('Invalid retained model label.')
            app, model_name = label.split('.')
            if _identifier(app) != app_label:
                raise RetainedSchemaError('Retained model has another app label.')
            _identifier(model_name)
            table = _identifier(entry['db_table'])
            if table in seen_tables or label in seen_labels:
                raise RetainedSchemaError('Duplicate retained schema contract.')
            seen_tables.add(table)
            seen_labels.add(label)
            meta = SchemaMeta(entry)
            model = SimpleNamespace(_meta=meta)
            if entry['scope'] not in {'global', 'organization'} or not isinstance(entry['columns'], list):
                raise RetainedSchemaError('Invalid retained table metadata.')
            field_names, field_columns = set(), set()
            for item in entry['columns']:
                kind = item['type']
                if kind not in _FIELD_TYPES:
                    raise RetainedSchemaError('Unsupported retained field type.')
                if item['name'] in field_names or item['column'] in field_columns:
                    raise RetainedSchemaError('Duplicate retained field.')
                if type(item['nullable']) is not bool or type(item['primary_key']) is not bool:
                    raise RetainedSchemaError('Invalid retained field flags.')
                field_names.add(item['name'])
                field_columns.add(item['column'])
                field = SchemaField(_identifier(item['name']), _identifier(item['column']), kind,
                                    item['nullable'], item['primary_key'], item['label'])
                relation = item.get('relation')
                if relation:
                    target_meta = SimpleNamespace(db_table=_identifier(relation['db_table']),
                                                  label_lower=relation['model_label'],
                                                  concrete_fields=[SimpleNamespace(name='organization')] if relation['organization'] else [])
                    field.related_model = SimpleNamespace(_meta=target_meta)
                    field.target_field = SchemaField('sysid', 'sysid', relation['target_type'], False, True, 'sysid')
                meta.concrete_fields.append(field)
                if field.primary_key:
                    if meta.pk is not None:
                        raise RetainedSchemaError('Multiple retained primary keys.')
                    meta.pk = field
            if meta.pk is None or meta.pk.name != 'sysid' or meta.pk.kind != 'UUIDField':
                raise RetainedSchemaError('Retained schema lacks UUID sysid.')
            operation = InstallRecordInfrastructure(app_label, model_name, entry['infrastructure'])
            # Forces exact column agreement before any expected SQL is generated.
            operation._audit_fields(model)
            operations[label] = operation
            specs.append(TableSpec(model=model, label=entry['label'], description=entry['description'],
                                   scope=entry['scope'], sensitive_fields=tuple(entry['sensitive_fields'])))
    return tuple(specs), operations
