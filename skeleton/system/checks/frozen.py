"""Frozen record infrastructure read from the migrations, and the SQL it would run."""

from django.conf import settings

from skeleton.common.lifecycle_operation import InstallUserLifecycleGuard
from skeleton.common.migration_operations import InstallRecordInfrastructure
from skeleton.common.technical_operations import GrantTechnicalTablePrivileges


class Capture:
    """Schema editor stand-in that records the SQL an operation would run."""

    def __init__(self, connection):
        self.connection = connection
        self.statements = []

    def quote_name(self, name):
        return self.connection.ops.quote_name(name)

    def execute(self, sql, params=None):
        self.statements.append(sql)


def frozen_operations(*, from_migrations=False):
    reference = getattr(settings, "SKELETON_FROZEN_INFRASTRUCTURE", None)
    if reference and not from_migrations:
        from ..infrastructure_snapshot import load_inventory

        return load_inventory(reference)
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None)
    result, technical, seen, lifecycle = {}, {}, set(), None
    for leaf in sorted(loader.graph.leaf_nodes()):
        for node in loader.graph.forwards_plan(leaf):
            if node in seen:
                continue
            seen.add(node)
            for operation in loader.disk_migrations[node].operations:
                if isinstance(operation, InstallRecordInfrastructure):
                    result[f"{operation.app_label}.{operation.model_name.lower()}"] = operation
                elif isinstance(operation, GrantTechnicalTablePrivileges):
                    technical.update({table: set(actions) for table, actions in operation.tables.items()})
                elif isinstance(operation, InstallUserLifecycleGuard):
                    lifecycle = operation
    return result, technical, lifecycle


def expected_sql(operation, model, connection):
    capture = Capture(connection)
    operation._postgres(capture, model)
    return capture.statements


_frozen_operations = frozen_operations
_expected_sql = expected_sql
