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


def _frozen_seal(operation):
    return operation.specification.get("seal"), operation.specification.get("seal_parent")


def seal_model_problems(model, operation=None, spec=None):
    """Declaration problems of a sealable model or of a child governed by a sealed parent (E040)."""
    from skeleton.common.field_names import ACCESS_FIELD_NAMES, SEAL_FIELD_NAMES, SYSTEM_FIELD_NAMES
    from skeleton.common.migration_operations import build_infrastructure_specification
    from skeleton.common.models import SealableMixin

    label = model._meta.label
    problems = []
    sealable = issubclass(model, SealableMixin)
    parent = getattr(model, "seal_parent", None)
    if sealable:
        columns = {field.name: field for field in model._meta.concrete_fields}
        by_attname = {field.attname: field for field in model._meta.concrete_fields}
        reserved = {*SEAL_FIELD_NAMES, *SYSTEM_FIELD_NAMES, *ACCESS_FIELD_NAMES, "organization", "organization_id"}
        if not model.seal_fields:
            problems.append(f"{label}: a sealable model must declare seal_fields.")
        for name in model.seal_fields:
            field = columns.get(name) or by_attname.get(name)
            if field is None:
                problems.append(f"{label}: seal_fields names {name!r}, which is not a concrete column.")
            elif {field.name, field.attname} & reserved:
                problems.append(f"{label}: seal_fields cannot name the system, access or seal column {name!r}.")
    if parent:
        try:
            field = model._meta.get_field(parent)
        except Exception:  # noqa: BLE001 - FieldDoesNotExist and friends: report, never crash the checks
            field = None
        target = getattr(field, "related_model", None) if field is not None else None
        if (
            field is None
            or not getattr(field, "many_to_one", False)
            or not isinstance(target, type)
            or not issubclass(target, SealableMixin)
            or target._meta.app_label != model._meta.app_label
        ):
            problems.append(f"{label}: seal_parent must be a foreign key to a sealable model of the same app.")
        elif parent not in model.immutable_fields and field.attname not in model.immutable_fields:
            problems.append(f"{label}: the seal_parent foreign key must be in immutable_fields.")
    if spec is not None:
        if getattr(spec, "seal", None) is not None and not sealable:
            problems.append(f"{label}: TableSpec.seal is declared but the model is not sealable.")
        if sealable and getattr(spec, "seal", None) is None:
            problems.append(f"{label}: a sealable model needs TableSpec.seal (SealPolicy).")
        if set(spec.write_fields) & set(SEAL_FIELD_NAMES):
            problems.append(f"{label}: the seal columns can never be in write_fields.")
    if operation is not None and spec is not None and not problems:
        expected = build_infrastructure_specification(model, spec)
        frozen_seal, frozen_parent = _frozen_seal(operation)
        if frozen_seal != expected.get("seal") or frozen_parent != expected.get("seal_parent"):
            problems.append(
                f"{label}: the seal declared in the model differs from the frozen migration specification; "
                "add a migration with record_infrastructure --write."
            )
    return problems


@register()
def seal_checks(app_configs=None, **kwargs):
    """E040: sealable models and sealed parents are declared coherently and frozen in migrations (#526)."""
    from skeleton.common.models import SealableMixin

    frozen, _, _ = _frozen_operations(from_migrations=True)
    specs = {spec.model: spec for spec in all_specs()}
    errors = []
    for model in apps.get_models():
        if not (issubclass(model, SealableMixin) or getattr(model, "seal_parent", None)):
            if model in specs and getattr(specs[model], "seal", None) is not None:
                errors.extend(
                    Error(problem, id="skeleton.E040") for problem in seal_model_problems(model, spec=specs[model])
                )
            continue
        operation = frozen.get(model._meta.label_lower)
        for problem in seal_model_problems(model, operation, specs.get(model)):
            errors.append(Error(problem, id="skeleton.E040"))
    return errors
