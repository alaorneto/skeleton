from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from skeleton.common.technical_operations import GrantTechnicalTablePrivileges
from skeleton.system.checks import frozen_operations
from skeleton.system.registry import all_specs
from skeleton.system.retained_schema import load_retained_contracts
from skeleton.system.synchronization import physical_tables
from skeleton.tenancy.context import system_context


class Command(BaseCommand):
    help = "Offline recovery: reinstall exact frozen guards/audit/RLS/ownership and technical grants."

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default")

    def handle(self, *args, **options):
        using = options["database"]
        connection = connections[using]
        if connection.vendor != "postgresql":
            raise CommandError("Infrastructure recovery requires PostgreSQL (skeleton.E025).")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            if cursor.fetchone()[0] != "skeleton_owner":
                raise CommandError("Infrastructure recovery requires the offline skeleton_owner role.")
        try:
            operations, technical, lifecycle = frozen_operations()
            retained, retained_operations = load_retained_contracts()
            operations.update(retained_operations)
            models = {spec.model._meta.label_lower: spec.model for spec in (*all_specs(), *retained)}
            present = set(physical_tables(connection))
            if any(model._meta.db_table not in present for model in models.values()):
                raise CommandError("A frozen own table is missing; do not recreate data during recovery.")
            if any(label not in operations for label in models):
                raise CommandError("Frozen record infrastructure is incomplete.")
            with connection.schema_editor() as editor:
                with system_context(reason="deployment:infrastructure-recovery", using=using):
                    for label, model in sorted(models.items()):
                        operations[label]._postgres(editor, model)
                    GrantTechnicalTablePrivileges(
                        {name: sorted(actions) for name, actions in technical.items()}
                    ).database_forwards("sk_system", editor, None, None)
                    if lifecycle:
                        lifecycle.database_forwards("sk_accounts", editor, None, None)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CommandError(f"Infrastructure recovery failed: {type(exc).__name__}") from None
        self.stdout.write(self.style.SUCCESS("Exact frozen record infrastructure reinstalled."))
