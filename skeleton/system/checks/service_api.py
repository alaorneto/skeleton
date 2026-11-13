"""Service API checks: the declared API (E018) and the import boundary (E019/W019)."""

from django.apps import apps
from django.core.checks import Error, Warning, register

from ..app_registry import all_apps


@register()
def service_api_checks(app_configs=None, **kwargs):
    """E018: every registered app publishes a well-formed service API (docs/service-api.md)."""
    from skeleton.service_api.contract import structural_problems
    from skeleton.service_api.registry import DeclaredService, ServiceDeclarationError, resolve_classes

    errors = []
    for manifest in all_apps():
        label = manifest.app_label
        if manifest.service_api is None:
            # Two-step obligation: historical signed manifests keep loading until the core that
            # makes runtime.service_api mandatory; new releases are refused by build and doctor.
            errors.append(
                Warning(
                    f"{label}: the manifest does not declare runtime.service_api; publish service_api.py and "
                    "docs/service-api.md before the next release.",
                    id="skeleton.W018",
                )
            )
            continue
        declaration = DeclaredService(
            label, manifest.app_id or label, manifest.service_api.module, manifest.service_api.classes
        )
        try:
            app_module = apps.get_app_config(label).name
        except LookupError:
            app_module = None
        if app_module and not (declaration.module == app_module or declaration.module.startswith(app_module + ".")):
            errors.append(
                Error(
                    f"{label}: service API module {declaration.module} is outside the app package.", id="skeleton.E018"
                )
            )
            continue
        try:
            classes = resolve_classes(declaration)
        except ServiceDeclarationError as exc:
            errors.append(Error(f"{label}: {exc}", id="skeleton.E018"))
            continue
        for name, service_class in classes.items():
            if getattr(service_class, "__module__", None) != declaration.module and not str(
                getattr(service_class, "__module__", "")
            ).startswith(declaration.module + "."):
                errors.append(Error(f"{label}: {name} must be defined in {declaration.module}.", id="skeleton.E018"))
                continue
            problems = structural_problems(service_class)
            if not problems and service_class.app_label != label:
                # app_label makes every method require the provider app enabled in the organization.
                problems = [f"app_label must be {label!r} so calls require the app enabled (Unavailable)."]
            for problem in problems:
                errors.append(Error(f"{label}: {name}: {problem}", id="skeleton.E018"))
    return errors


def _boundary_providers(manifests):
    from ..import_boundary import Provider

    providers = {}
    for label, manifest in manifests.items():
        try:
            app_module = apps.get_app_config(label).name
        except LookupError:
            continue
        packages = {app_module}
        if manifest.package_name:
            packages.add(f"applications.{manifest.package_name}")
        services = set()
        if manifest.service_api is not None:
            module = manifest.service_api.module
            services.add(module)
            for package in packages:
                if module.startswith(app_module + "."):
                    services.add(package + module[len(app_module) :])
        providers[label] = Provider(
            identity=label, packages=tuple(sorted(packages)), service_modules=tuple(sorted(services))
        )
    return providers


@register()
def import_boundary_checks(app_configs=None, **kwargs):
    """E019/W019: apps import other apps only through their declared service_api (docs/service-api.md)."""
    from ..app_registry import get_app
    from ..import_boundary import scan_application

    manifests = {manifest.app_label: manifest for manifest in all_apps()}
    providers = _boundary_providers(manifests)
    errors = []
    for label in sorted(manifests):
        if label not in providers:
            continue
        config = apps.get_app_config(label)
        own = providers[label]
        # get_app() resolves dependency app IDs to registered labels.
        dependencies = set(get_app(label).dependencies)
        violations = scan_application(
            config.path,
            package=config.name,
            aliases=[package for package in own.packages if package != config.name],
            providers=[provider for other, provider in providers.items() if other != label],
            dependencies=dependencies,
        )
        for violation in violations:
            message = f"{label}: {violation.path}:{violation.line}: {violation.message}"
            if violation.is_error:
                errors.append(Error(message, id="skeleton.E019"))
            else:
                # Non-public core surfaces are a warning first, an error in a later core.
                errors.append(Warning(message, id="skeleton.W019"))
    return errors
