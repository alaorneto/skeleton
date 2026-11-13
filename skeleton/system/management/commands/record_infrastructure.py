"""Author aid: list or write the missing ``InstallRecordInfrastructure`` operations of an app.

The generated specification is frozen into a NEW migration; existing migrations are
never edited and DDL keeps reading only the frozen specification and historical models.
"""

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.core.management.utils import run_formatters
from django.db import migrations
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.writer import MigrationWriter

from skeleton.common.migration_operations import InstallRecordInfrastructure, build_infrastructure_specification
from skeleton.system.app_registry import CORE_APP_LABELS
from skeleton.system.checks import frozen_operations
from skeleton.system.registry import all_specs

AUDIT_DEPENDENCY = ("sk_audit", "0002_record_infrastructure")


def _differences(frozen, specification, model):
    generated = InstallRecordInfrastructure(model._meta.app_label, model._meta.model_name, specification)
    current, reasons = frozen.specification, []
    if current.get("scope") != specification["scope"]:
        reasons.append("scope")
    if set(current.get("immutable_fields", ())) != set(specification.get("immutable_fields", ())):
        reasons.append("immutable_fields")
    if frozen._append_only(model) != generated._append_only(model):
        reasons.append("append_only")
    columns = current.get("columns")
    if columns is None or {column["name"] for column in columns} != {
        column["name"] for column in specification["columns"]
    }:
        reasons.append("columns")
    elif dict(frozen._audit_fields(model)) != dict(generated._audit_fields(model)):
        reasons.append("sensitive")
    return reasons


def pending_infrastructure(app_label, frozen=None):
    """Return ``[(spec, specification, reasons)]`` for models whose frozen infrastructure is absent or stale."""
    if frozen is None:
        frozen = frozen_operations(from_migrations=True)[0]
    pending = []
    for spec in all_specs():
        model = spec.model
        if model._meta.app_label != app_label:
            continue
        specification = build_infrastructure_specification(model, spec)
        operation = frozen.get(model._meta.label_lower)
        reasons = ["missing"] if operation is None else _differences(operation, specification, model)
        if reasons:
            pending.append((spec, specification, reasons))
    return pending


class Command(BaseCommand):
    help = "List models of an app without current frozen record infrastructure; --write creates a new migration."
    # This command repairs what checks such as E016 report, so those checks must not block it.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("app_label")
        parser.add_argument("--write", action="store_true", help="Create a new migration with the missing operations.")

    def handle(self, *args, **options):
        app_label = options["app_label"]
        try:
            apps.get_app_config(app_label)
        except LookupError:
            raise CommandError(f"Unknown app label: {app_label}") from None
        pending = pending_infrastructure(app_label)
        if not pending:
            self.stdout.write(f"No pending record infrastructure for {app_label}.")
            return
        for spec, _, reasons in pending:
            self.stdout.write(f"{spec.model._meta.label_lower}: {', '.join(reasons)}")
        if options["write"]:
            path = self._write(app_label, pending)
            self.stdout.write(self.style.SUCCESS(f"Created {path}"))

    def _write(self, app_label, pending):
        if app_label in CORE_APP_LABELS:
            raise CommandError("Core migrations are owned by the skeleton distribution; do not generate them here.")
        loader = MigrationLoader(None, ignore_no_migrations=True)
        leaves = loader.graph.leaf_nodes(app_label)
        if not leaves:
            raise CommandError(f"{app_label} has no migrations; run makemigrations first.")
        if len(leaves) > 1:
            raise CommandError(f"{app_label} has conflicting leaf migrations; run makemigrations --merge first.")
        leaf = leaves[0]
        state = loader.project_state(leaf)
        operations = []
        for spec, specification, _ in pending:
            model = spec.model
            try:
                historical = state.apps.get_model(app_label, model._meta.model_name)
            except LookupError:
                raise CommandError(f"{model._meta.label}: not in migrations; run makemigrations first.") from None
            if {field.column for field in historical._meta.concrete_fields} != {
                column["name"] for column in specification["columns"]
            }:
                raise CommandError(f"{model._meta.label}: model changes pending; run makemigrations first.")
            operations.append(InstallRecordInfrastructure(app_label, model._meta.model_name, specification))
        number = (MigrationAutodetector.parse_number(leaf[1]) or 0) + 1
        name = f"{number:04d}_record_infrastructure"
        if (app_label, name) in loader.disk_migrations:
            raise CommandError(f"Migration {app_label}.{name} already exists.")
        migration = migrations.Migration(name, app_label)
        migration.dependencies = sorted({leaf, AUDIT_DEPENDENCY} - {(app_label, name)})
        migration.operations = operations
        writer = MigrationWriter(migration)
        # Exclusive creation: never overwrite an existing migration file.
        with open(writer.path, "x", encoding="utf-8") as stream:
            stream.write(writer.as_string())
        run_formatters([writer.path], stderr=self.stderr)
        return writer.path
