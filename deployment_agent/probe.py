"""Executed only in a verified prepared release, never in the agent/web process."""

import json
import os
from pathlib import Path
import sys


def main():
    import django
    django.setup()
    from django.apps import apps
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.db.migrations.recorder import MigrationRecorder

    executor = MigrationExecutor(connection)
    executor.loader.check_consistent_history(connection)
    targets = executor.loader.graph.leaf_nodes()
    inventory = json.loads(Path(os.environ["SKELETON_DEPLOYMENT_INVENTORY"]).read_bytes())
    for entry in inventory.get("apps", []):
        manifest = entry["manifest"]
        expected = sorted(manifest["migrations"]["leaves"])
        actual = sorted(name for label, name in targets if label == manifest["app_label"])
        if expected != actual:
            raise ValueError("migration_leaf_mismatch")
    plan = executor.migration_plan(targets)
    retained_labels = {entry["manifest"]["app_label"] for entry in inventory.get("apps", []) if entry["state"] == "schema_retained"}
    if any(migration.app_label in retained_labels for migration, _ in plan):
        raise ValueError("retained_migration_not_applied")
    if any(backwards for migration, backwards in plan):
        raise ValueError("backwards_migration_forbidden")
    applied = sorted([list(key) for key in MigrationRecorder(connection).applied_migrations()])
    migrations = []
    for migration, backwards in plan:
        operations = []
        for operation in migration.operations:
            operations.append({"type": type(operation).__name__, "reversible": bool(operation.reversible)})
        migrations.append({"app_label": migration.app_label, "name": migration.name, "backwards": backwards, "atomic": migration.atomic, "operations": operations})
    Installation = apps.get_model("sk_system", "AppInstallation")
    Organization = apps.get_model("sk_tenancy", "Organization")
    labels = [entry["manifest"]["app_label"] for entry in inventory.get("apps", [])]
    impact = {"organizations": Organization.all_objects.filter(deleted_at__isnull=True).count(), "configured_installations": Installation.all_objects.filter(app_label__in=labels, deleted_at__isnull=True).count()}
    # Output file, not stdout: imports/checks may emit unrelated diagnostics.
    Path(sys.argv[1]).write_text(json.dumps({"migrations": migrations, "applied": applied, "targets": [list(target) for target in sorted(targets)], "impact": impact}, sort_keys=True))


if __name__ == "__main__":
    main()
