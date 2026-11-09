"""Service APIs declared by registered apps (``runtime.service_api``), for checks and documentation.

The declaration lives in each ``AppManifest``; this module only resolves it. Resolution imports
the declared module and looks up the named classes, so it runs after app loading (checks,
documentation pages), never inside ``AppConfig.ready()``.
"""

import importlib
from dataclasses import dataclass

from .base import ServiceAPI


class ServiceDeclarationError(LookupError):
    """The declared module or class cannot be resolved."""


@dataclass(frozen=True, slots=True)
class DeclaredService:
    app_label: str
    app_id: str
    module: str
    classes: tuple[str, ...]


def declared_services() -> tuple[DeclaredService, ...]:
    """Every registered app that declares runtime.service_api, ordered by app label."""
    from skeleton.system.app_registry import all_apps

    return tuple(
        DeclaredService(
            app_label=manifest.app_label,
            app_id=manifest.app_id or manifest.app_label,
            module=manifest.service_api.module,
            classes=manifest.service_api.classes,
        )
        for manifest in all_apps()
        if manifest.service_api is not None
    )


def resolve_classes(declaration: DeclaredService) -> dict[str, object]:
    """Import the declared module and return name -> attribute for every declared class."""
    try:
        module = importlib.import_module(declaration.module)
    except Exception as exc:  # Any import failure leaves the declaration unresolvable.
        raise ServiceDeclarationError(
            f"service API module {declaration.module} cannot be imported ({type(exc).__name__}: {exc})."
        ) from exc
    result: dict[str, object] = {}
    for name in declaration.classes:
        if not hasattr(module, name):
            raise ServiceDeclarationError(f"{declaration.module} does not define {name}.")
        result[name] = getattr(module, name)
    return result


def service_registry() -> dict[str, tuple[type[ServiceAPI], ...]]:
    """app_id -> resolved service classes; declarations that fail to resolve are left out (see E018)."""
    registry: dict[str, tuple[type[ServiceAPI], ...]] = {}
    for declaration in declared_services():
        try:
            classes = resolve_classes(declaration)
        except ServiceDeclarationError:
            continue
        registry[declaration.app_id] = tuple(
            value for value in classes.values() if isinstance(value, type) and issubclass(value, ServiceAPI)
        )
    return registry
