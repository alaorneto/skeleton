"""Static import boundary between apps (docs/service-api.md, "Publicar só por ela").

An app may import from another app only the ``service_api`` module that app declares, and only
when the manifest lists that app in ``dependencies``. From the core it may import only the public
surfaces of the specification. The analysis parses source with ``ast``; it never imports app code,
so the system check (E019/W019) and ``package doctor`` share it. Dynamic imports (``importlib``,
``__import__``), attribute access after ``import skeleton.x as m`` and ``import *`` are outside
its reach and remain a code-review concern.
"""

import ast
import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

# Public core surfaces an app may import (docs/service-api.md, "Superfícies públicas do núcleo").
PUBLIC_CORE_PACKAGES = ("skeleton.service_api", "skeleton.testing")
# The closed list of public names of each public core module (#300). None marks a module the
# specification publishes as a set of builders (schema, flow steps): any public name of it.
PUBLIC_CORE_NAMES: dict[str, frozenset[str] | None] = {
    "skeleton.common.models": frozenset({"SystemRecord", "OrganizationRecord", "SecuredRecord"}),
    "skeleton.common.migration_operations": frozenset({"InstallRecordInfrastructure", "InstallSearchIndex"}),
    "skeleton.system.app_registry": frozenset({"AppManifest", "register_app"}),
    "skeleton.system.registry": frozenset({"TableSpec", "register"}),
    "skeleton.system.record_operations": frozenset({"RecordOperations", "register_record_operations"}),
    "skeleton.access.policies": frozenset({"authorized_queryset", "require_record_action", "has_table_permission"}),
    "skeleton.tenancy.context": frozenset({"current_context"}),
    "skeleton.web.views": frozenset({"tenant_page"}),
    "skeleton.api.views": frozenset({"TenantAPI", "request_fields"}),
    # Operation and schema builders for the ``openapi`` attribute of an app's API views (#178).
    "skeleton.api.openapi": None,
    # Registration in AppConfig.ready() (#84): the documented way to declare types of an app.
    "skeleton.jobs.registry": frozenset({"JobType", "register_job_type"}),
    "skeleton.jobs.periodic": frozenset({"register_periodic"}),
    "skeleton.events.registry": frozenset({"EventType", "register_event_type", "register_consumer"}),
    "skeleton.notifications.registry": frozenset({"NotificationType", "register_notification_type"}),
    "skeleton.sequences.registry": frozenset({"SequenceSpec", "register_sequence"}),
    # Flow declarations (docs/flows.md): specification builders and register_flow*.
    "skeleton.flows.registry": None,
    "skeleton.integrations.tokens.scopes": frozenset({"register_token_scope"}),
    "skeleton.tenancy.aggregate": frozenset({"aggregate_route", "for_each_organization", "user_context"}),
    # Transitional until ApplicationService (#105) replaces them.
    "skeleton.system.applications": frozenset({"application_state", "require_application_enabled"}),
}
PUBLIC_CORE_MODULES = frozenset(PUBLIC_CORE_NAMES)
# Every importable directory is analysed (#300): only caches and dot-directories are not.
SKIPPED_DIRECTORIES = frozenset({"__pycache__"})
MAX_SOURCE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Provider:
    """Another app as seen by the analysis: its package names, declared service module and identity."""

    identity: str
    packages: tuple[str, ...]
    service_modules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Violation:
    """``code`` is ``internal_import``, ``undeclared_dependency``, ``core_import`` or ``unreadable_source``."""

    code: str
    path: str
    line: int
    module: str
    message: str
    provider: str | None = None

    @property
    def is_error(self) -> bool:
        return self.code != "core_import"


def _within(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _module_name(relative: Path, package: str) -> tuple[str, bool]:
    """Dotted module of a source file and whether it is a package ``__init__``."""
    parts = list(relative.with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join([package, *parts]), is_package


def _resolve_relative(node: ast.ImportFrom, module: str, is_package: bool) -> str | None:
    base = module.split(".") if is_package else module.split(".")[:-1]
    if node.level - 1 >= len(base):
        return None  # Beyond the top-level package: Python refuses it too.
    anchor = base[: len(base) - (node.level - 1)]
    return ".".join([*anchor, *([node.module] if node.module else [])]) or None


def imported_modules(tree: ast.AST, module: str, *, is_package: bool = False) -> Iterator[tuple[int, str, str]]:
    """(line, module, candidate submodule) for every import; relative imports resolved against module.

    ``from a.b import c`` yields ``("a.b", "a.b.c")`` because ``c`` may be a submodule; ``import a.b``
    yields ``("a.b", "a.b")``.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, alias.name
        elif isinstance(node, ast.ImportFrom):
            target = node.module if node.level == 0 else _resolve_relative(node, module, is_package)
            if target is None:
                continue
            for alias in node.names:
                yield node.lineno, target, target if alias.name == "*" else f"{target}.{alias.name}"


def _is_public_core(module: str, candidate: str) -> bool:
    for name in (candidate, module):
        if name in PUBLIC_CORE_MODULES or any(_within(name, package) for package in PUBLIC_CORE_PACKAGES):
            return True
    return False


def core_model_lookups(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """(line, label) of every ``<anything>.get_model("sk_...", ...)`` call with a literal core label (#300)."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_model"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.split(".")[0].startswith("sk_")
        ):
            yield node.lineno, node.args[0].value


def _core_name_violation(module: str, candidate: str) -> tuple[str, str, Provider | None] | None:
    """The name rules of a core import (#300): private names and names outside the closed list."""
    name = candidate[len(module) + 1 :] if candidate.startswith(module + ".") else ""
    if any(part.startswith("_") and not part.startswith("__") for part in candidate.split(".")):
        return ("core_import", f"{candidate} is private to the core; private names are not a contract.", None)
    if _is_public_core(module, candidate):
        allowed = PUBLIC_CORE_NAMES.get(module)
        if allowed is not None and name and name not in allowed:
            return (
                "core_import",
                f"{name} is not a public name of {module}; apps may import only {sorted(allowed)}.",
                None,
            )
        return None
    return (
        "core_import",
        f"{module} is not a public core surface for apps; use skeleton.service_api or the listed surfaces.",
        None,
    )


def classify_import(
    module: str,
    candidate: str,
    *,
    own_packages: Iterable[str],
    providers: Iterable[Provider],
    dependencies: Iterable[str],
) -> tuple[str, str, Provider | None] | None:
    """(code, message, provider) when one import crosses the boundary, else None."""
    own = tuple(own_packages)
    providers = tuple(providers)
    packages = [package for provider in providers for package in provider.packages]
    if module == "applications" or any(
        _within(candidate, package) and not _within(module, package) for package in packages
    ):
        module = candidate  # from applications import tasks / from examples import notes
    if any(_within(module, package) for package in own):
        return None
    # Providers first (#300): a first-party app under skeleton.contrib follows the app rules.
    for provider in providers:
        if not any(_within(module, package) for package in provider.packages):
            continue
        allowed = any(_within(name, service) for service in provider.service_modules for name in (candidate, module))
        if not allowed:
            return (
                "internal_import",
                f"{module} is internal to {provider.identity}; import only its declared service_api.",
                provider,
            )
        if provider.identity not in set(dependencies):
            return (
                "undeclared_dependency",
                f"{module} belongs to {provider.identity}, which the manifest does not declare in dependencies.",
                provider,
            )
        return None
    parts = module.split(".")
    if parts[:2] == ["skeleton", "contrib"] and len(parts) > 2:
        # A first-party app this analysis does not know (package doctor): only its service_api.
        package = ".".join(parts[:3])
        if not any(_within(name, package + ".service_api") for name in (candidate, module)):
            return (
                "internal_import",
                f"{module} is internal to {package}; import only its service_api.",
                Provider(identity=package, packages=(package,), service_modules=(package + ".service_api",)),
            )
        return None
    if _within(module, "skeleton"):
        return _core_name_violation(module, candidate)
    if parts[0] == "applications" and len(parts) > 1:
        # An app this analysis does not know (package doctor without inventory): the conventional
        # applications.<package>.service_api is the only module it could publish.
        package = ".".join(parts[:2])
        if not any(_within(name, package + ".service_api") for name in (candidate, module)):
            return (
                "internal_import",
                f"{module} is internal to {package}; import only its service_api.",
                Provider(identity=package, packages=(package,), service_modules=(package + ".service_api",)),
            )
    return None


def iter_sources(root: Path) -> Iterator[Path]:
    """Python sources of an app root in a stable order, without following links."""
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(
            name for name in directories if name not in SKIPPED_DIRECTORIES and not name.startswith(".")
        )
        for name in sorted(filenames):
            path = Path(current) / name
            if name.endswith(".py") and not path.is_symlink():
                yield path


def scan_application(
    root: Path | str,
    *,
    package: str,
    aliases: Iterable[str] = (),
    providers: Iterable[Provider],
    dependencies: Iterable[str],
) -> list[Violation]:
    """Every boundary violation in the sources under root, whose import name is package.

    aliases are other names of the same package (``applications.<package_name>`` for an app
    loaded from another location). Unparsable files are reported, never skipped silently.
    """
    root = Path(root).resolve()
    own = (package, *aliases)
    provider_list = [item for item in providers if not any(_within(name, p) for name in item.packages for p in own)]
    declared = tuple(dependencies)
    violations: list[Violation] = []
    for path in iter_sources(root):
        relative = path.relative_to(root)
        display = relative.as_posix()
        try:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError("source too large")
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=display)
        except (SyntaxError, UnicodeDecodeError, ValueError, OSError) as exc:
            violations.append(
                Violation("unreadable_source", display, 0, "", f"Source cannot be analysed ({type(exc).__name__}).")
            )
            continue
        module, is_package = _module_name(relative, package)
        for line, imported, candidate in imported_modules(tree, module, is_package=is_package):
            result = classify_import(
                imported, candidate, own_packages=own, providers=provider_list, dependencies=declared
            )
            if result is not None:
                code, message, provider = result
                violations.append(
                    Violation(code, display, line, imported, message, provider.identity if provider else None)
                )
        if "migrations" not in relative.parts:  # Historical models are mandatory in migrations.
            for line, label in core_model_lookups(tree):
                violations.append(
                    Violation(
                        "core_import",
                        display,
                        line,
                        f"get_model({label!r})",
                        "apps.get_model reaches a core model; use skeleton.service_api instead.",
                    )
                )
    return violations


def providers_from_manifests(manifests: Mapping[str, Mapping[str, object]]) -> list[Provider]:
    """Providers from canonical app manifests (app_id -> manifest), as package doctor sees them."""
    result = []
    for identity, manifest in sorted(manifests.items()):
        package = manifest.get("package_name")
        if not isinstance(package, str):
            continue
        runtime = manifest.get("runtime")
        declared = runtime.get("service_api") if isinstance(runtime, Mapping) else None
        service = declared.get("module") if isinstance(declared, Mapping) else None
        result.append(
            Provider(
                identity=identity,
                packages=(f"applications.{package}",),
                service_modules=(service,) if isinstance(service, str) else (),
            )
        )
    return result
