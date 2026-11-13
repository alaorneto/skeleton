"""Consistency of app extensions: declared migration dependencies and relations."""

from django.apps import apps
from django.core.checks import Error, Warning, register

from ..app_registry import all_apps
from ..registry import all_specs
from .frozen import _frozen_operations


def undeclared_migration_dependencies(migrations, registered, allowed):
    """Registered app labels the migrations depend on outside the allowed (declared) set."""
    return sorted(
        {
            dependency[0]
            for migration in migrations
            for dependency in migration.dependencies
            if dependency[0] in registered and dependency[0] not in allowed
        }
    )


def undeclared_relations(models_of_app, registered, allowed):
    """(model, field, target app) for relations to registered apps outside the allowed set."""
    return sorted(
        {
            (model._meta.label, field.name, field.related_model._meta.app_label)
            for model in models_of_app
            for field in model._meta.concrete_fields
            if field.is_relation
            and isinstance(field.related_model, type)
            and field.related_model._meta.app_label in registered
            and field.related_model._meta.app_label not in allowed
        }
    )


@register()
def extension_consistency_checks(app_configs=None, **kwargs):
    """Database-free consistency between what an app declares and what its code does."""
    from django.db.migrations.loader import MigrationLoader

    from ..app_registry import get_app, required_labels

    manifests = {manifest.app_label: manifest for manifest in all_apps()}
    if not manifests:
        return []
    errors = []
    loader = MigrationLoader(None, ignore_no_migrations=True)
    for label in sorted(manifests):
        allowed = {label, *required_labels(label)}
        migrations = [migration for key, migration in loader.disk_migrations.items() if key[0] == label]
        for other in undeclared_migration_dependencies(migrations, manifests, allowed):
            errors.append(
                Error(
                    f"{label}: migrations depend on {other}, which the manifest does not declare as a dependency.",
                    id="skeleton.E014",
                )
            )
        try:
            models_of_app = list(apps.get_app_config(label).get_models())
        except LookupError:
            models_of_app = []
        for model_label, field_name, other in undeclared_relations(models_of_app, manifests, allowed):
            errors.append(
                Error(
                    f"{model_label}.{field_name} references {other}, which the manifest does not declare as a dependency.",
                    id="skeleton.E015",
                )
            )
        canonical = getattr(get_app(label), "canonical", None)
        if canonical is not None:
            declared = set(canonical["migrations"]["leaves"])
            actual = {name for app_label, name in loader.graph.leaf_nodes(label) if app_label == label}
            if declared != actual:
                # A warning: authors run makemigrations before updating the manifest. build, doctor
                # and the deployment probe still refuse mismatched leaves.
                errors.append(
                    Warning(
                        f"{label}: manifest migrations.leaves {sorted(declared)} differ from the migration graph "
                        f"leaves {sorted(actual)}.",
                        id="skeleton.W017",
                    )
                )
    frozen, _, _ = _frozen_operations(from_migrations=True)
    for spec in all_specs():
        model = spec.model
        if model._meta.app_label not in manifests:
            continue
        operation = frozen.get(model._meta.label_lower)
        if operation is None:
            continue  # check_skeleton_schema reports missing infrastructure with database context.
        declared = set(operation.specification.get("sensitive_fields", ()))
        if declared != set(spec.sensitive_fields):
            errors.append(
                Error(
                    f"{model._meta.label}: TableSpec sensitive_fields {sorted(spec.sensitive_fields)} differ from the "
                    f"frozen migration specification {sorted(declared)}; add a migration with the new specification.",
                    id="skeleton.E016",
                )
            )
        personal = set(operation.specification.get("personal_fields", ()))
        if personal != set(getattr(spec, "personal_fields", ()) or ()):
            errors.append(
                Error(
                    f"{model._meta.label}: TableSpec personal_fields differ from the frozen migration specification "
                    f"{sorted(personal)}; add a migration with the new specification.",
                    id="skeleton.E032",
                )
            )
    return errors
