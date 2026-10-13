import json
from pathlib import Path

from django.apps import apps
from django.db import connections, router, transaction
from django.db import models

from skeleton.tenancy.context import system_context
from .registry import all_specs
from .app_registry import CORE_APP_LABELS


def technical_manifest():
    return json.loads(Path(__file__).with_name("technical_tables.json").read_text())


def physical_tables(connection):
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='skeleton' ORDER BY tablename")
            return [row[0] for row in cursor.fetchall()]
        return connection.introspection.table_names(cursor)


def _reconcile(model, lookup, physical, defaults, using):
    record = model.all_objects.using(using).filter(**lookup).first()
    if record is None:
        record = model(**lookup, **defaults, **physical)
        record.save(using=using)
    else:
        changed = set()
        for name, value in physical.items():
            if getattr(record, name) != value:
                setattr(record, name, value)
                changed.add(name)
        if changed:
            record.save(using=using, update_fields=changed)
    return record


def sync_table_catalog(*, using="default", apps_registry=None):
    from .models import TableDefinition, FieldDefinition
    from skeleton.tenancy.models import Organization
    registry = apps_registry or apps
    connection = connections[using]
    if connection.vendor not in ("sqlite", "postgresql"):
        raise ValueError("Catalog supports SQLite and PostgreSQL only.")
    if not router.allow_migrate_model(using, TableDefinition):
        return {"tables": 0, "fields": 0}
    names = physical_tables(connection)
    if TableDefinition._meta.db_table not in names or FieldDefinition._meta.db_table not in names:
        return {"tables": 0, "fields": 0}
    schema = "skeleton" if connection.vendor == "postgresql" else "main"
    specs = {spec.model._meta.db_table: spec for spec in all_specs()}
    from .retained_schema import load_retained_contracts
    retained_specs, _ = load_retained_contracts()
    for spec in retained_specs:
        if spec.model._meta.db_table in specs:
            raise ValueError("Retained schema overlaps an installed model.")
        specs[spec.model._meta.db_table] = spec
    technical = technical_manifest()
    known = {}
    for model in registry.get_models(include_auto_created=True):
        if not model._meta.proxy and not model._meta.swapped:
            known.setdefault(model._meta.db_table, model)
    for spec in retained_specs:
        known[spec.model._meta.db_table] = spec.model
    field_count = 0
    with system_context(reason="migrate:catalog", using=using):
        for name in names:
            model, spec = known.get(name), specs.get(name)
            is_owned = spec is not None
            origin = "core" if is_owned and model and model._meta.app_label in CORE_APP_LABELS else "application" if is_owned else "framework" if name in technical and (name.startswith("django_") or name.startswith("auth_")) else "third_party" if name in technical else "unclassified"
            scope = spec.scope if spec else "technical"
            fields = {field.column: field for field in model._meta.concrete_fields} if model else {}
            uuid_managed = bool(model and model._meta.pk.get_internal_type() == "UUIDField" and model._meta.pk.name == "sysid")
            base_class = "SecuredRecord" if is_owned and any(f.name == "visibility" for f in model._meta.fields) else "OrganizationRecord" if is_owned and scope == "organization" else "SystemRecord" if is_owned else ""
            table = _reconcile(TableDefinition, {"database_alias": using, "schema_name": schema, "db_table": name},
                {"model_label": model._meta.label_lower if model else None, "origin": origin, "scope": scope,
                 "base_class": base_class, "uuid_managed": uuid_managed, "soft_delete": is_owned,
                 "audit_mode": "append_only" if model and model._meta.label_lower == "sk_audit.auditevent" else "trigger" if is_owned else "library" if name in technical else "none",
                 "api_exposed": bool(spec and spec.expose_api), "available": True},
                {"label": spec.label or str(model._meta.verbose_name) if spec else str(model._meta.verbose_name) if model else name,
                 "description": spec.description if spec else technical.get(name, "Tabela física sem classificação; requer revisão.")}, using)
            with connection.cursor() as cursor:
                description = connection.introspection.get_table_description(cursor, name)
                constraints = connection.introspection.get_constraints(cursor, name)
            primary_columns = {column for constraint in constraints.values() if constraint.get("primary_key") for column in constraint["columns"]}
            seen = []
            for column in description:
                field = fields.get(column.name)
                seen.append(column.name)
                type_name = field.get_internal_type() if field else connection.introspection.get_field_type(column.type_code, column)
                sensitive = bool(spec and field and field.name in spec.sensitive_fields)
                _reconcile(FieldDefinition, {"table": table, "column": column.name},
                    {"name": field.name if field else column.name, "type_name": type_name, "nullable": column.null_ok,
                     "primary_key": column.name in primary_columns, "relation_target": field.related_model._meta.label_lower if field and field.is_relation else None,
                     "sensitive": sensitive, "available": True},
                    {"label": str(field.verbose_name) if field else column.name, "description": ""}, using)
                field_count += 1
            for missing in FieldDefinition.all_objects.using(using).filter(table=table, available=True).exclude(column__in=seen):
                missing.available = False
                missing.save(using=using, update_fields={"available"})
        for missing in TableDefinition.all_objects.using(using).filter(database_alias=using, schema_name=schema, available=True).exclude(db_table__in=names):
            missing.available = False
            missing.save(using=using, update_fields={"available"})
            for field in FieldDefinition.all_objects.using(using).filter(table=missing, available=True):
                field.available = False
                field.save(using=using, update_fields={"available"})
        organization_ids = list(Organization.all_objects.using(using).filter(is_active=True, deleted_at__isnull=True).values_list("sysid", flat=True)) if Organization._meta.db_table in names else []
        def reconcile():
            from skeleton.access.services import reconcile_builtin_permissions
            for organization_id in organization_ids:
                reconcile_builtin_permissions(organization_id=organization_id, using=using)
        transaction.on_commit(reconcile, using=using)
    return {"tables": len(names), "fields": field_count}


def post_migrate_catalog(sender, using, apps, **kwargs):
    if sender.label == "sk_system":
        sync_table_catalog(using=using, apps_registry=apps)
