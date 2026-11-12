"""Direction of imports between core packages (docs/architecture.md).

The exception list only shrinks: a new violation fails, and so does a listed
exception that no longer exists (delete its line).
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KERNEL = {"accounts", "tenancy", "access", "system", "audit"}
SURFACES = {"web", "api"}
UNRESTRICTED = {"web", "api", "setup", "conf", "contrib", "testing"}

# (file, imported module): reason and the issue that removes it.
KNOWN_EXCEPTIONS = {
    ("skeleton/common/models.py", "skeleton.tenancy.models"): "owner membership check",
    ("skeleton/common/responses.py", "skeleton.files"): "private download response",
    ("skeleton/system/application_views.py", "skeleton.api.errors"): "app views live in system",
    ("skeleton/system/application_views.py", "skeleton.api.views"): "app views live in system",
    ("skeleton/system/application_views.py", "skeleton.web.views"): "app views live in system",
    ("skeleton/system/application_views.py", "skeleton.service_api.applications"): "app views live in system",
    ("skeleton/system/checks.py", "skeleton.service_api.contract"): "check E018 inspects the declared API",
    ("skeleton/system/checks.py", "skeleton.service_api.registry"): "check E018 inspects the declared API",
    ("skeleton/tenancy/aggregate.py", "skeleton.service_api.errors"): "skips organizations denied by a service",
    ("skeleton/api/serializers.py", "skeleton.web.services"): "record gateway lives in web",
    ("skeleton/api/views.py", "skeleton.web.services"): "record gateway lives in web",
    ("skeleton/api/views.py", "skeleton.web.references"): "record gateway lives in web",
    ("skeleton/web/forms.py", "skeleton.api.serializers"): "dedicated resources live in api",
    ("skeleton/web/platform/views.py", "skeleton.api.serializers"): "audit projection lives in api",
    ("skeleton/web/urls.py", "skeleton.api.health"): "health probe lives in api",
    ("skeleton/web/views.py", "skeleton.api.serializers"): "dedicated resources live in api",
    ("skeleton/web/views.py", "skeleton.api.errors"): "validation conflict lives in api",
    ("skeleton/api/openapi.py", "skeleton.web.references"): "record gateway lives in web",
    ("skeleton/api/openapi.py", "skeleton.web.services"): "record gateway lives in web",
    ("skeleton/comments/apps.py", "skeleton.web.timeline"): "timeline registry lives in web",
    ("skeleton/comments/services.py", "skeleton.api.openapi"): "OpenAPI DTO markers live in api",
    ("skeleton/comments/timeline.py", "skeleton.web.timeline"): "timeline registry lives in web",
    ("skeleton/common/locale.py", "skeleton.tenancy.organization_settings"): "organization locale settings",
    ("skeleton/common/migration_operations.py", "skeleton.system.search"): "search index DDL lives in system",
    ("skeleton/files/services.py", "skeleton.api.openapi"): "OpenAPI DTO markers live in api",
    ("skeleton/system/application_views.py", "skeleton.api.openapi"): "app views live in system",
    ("skeleton/transfer/audit_export.py", "skeleton.api.serializers"): "audit projection lives in api",
    ("skeleton/transfer/exports.py", "skeleton.web.services"): "record gateway lives in web",
    ("skeleton/transfer/imports.py", "skeleton.web.services"): "record gateway lives in web",
}


def _package(module):
    parts = module.split(".")
    return parts[1] if parts[0] == "skeleton" and len(parts) > 1 else None


def _imports(tree, module_name):
    def walk(node, typing_only):
        for child in ast.iter_child_nodes(node):
            test = getattr(child, "test", None)
            guarded = typing_only or (
                isinstance(child, ast.If) and getattr(test, "id", getattr(test, "attr", "")) == "TYPE_CHECKING"
            )
            if not guarded and isinstance(child, ast.Import):
                yield from (alias.name for alias in child.names)
            elif not guarded and isinstance(child, ast.ImportFrom):
                if child.level:
                    base = module_name.split(".")[: -child.level]
                    target = ".".join(base + ([child.module] if child.module else []))
                else:
                    target = child.module or ""
                if target == "skeleton":
                    yield from ("skeleton." + alias.name for alias in child.names)
                else:
                    yield target
            yield from walk(child, guarded)

    yield from walk(tree, False)


def _violates(source, target, filename):
    if target in (None, source):
        return False
    if source == "common":
        return True  # R1
    if source in KERNEL and target in SURFACES | {"service_api", "contrib"}:
        return True  # R2
    if {source, target} == SURFACES:
        return True  # R3
    if source == "service_api" and target in SURFACES:
        return True  # R4
    surface_module = "views" in filename or "urls" in filename
    return target in SURFACES and source not in UNRESTRICTED and not surface_module  # R5


def _found():
    found = set()
    for path in sorted((ROOT / "skeleton").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if "/migrations/" in relative or relative.count("/") < 2:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        source = relative.split("/")[1]
        for module in _imports(tree, relative[:-3].replace("/", ".")):
            if _violates(source, _package(module), path.name):
                found.add((relative, module))
    return found


def test_no_new_layering_violation():
    assert sorted(_found() - set(KNOWN_EXCEPTIONS)) == []


def test_known_exceptions_still_exist():
    # A fixed exception must be deleted from KNOWN_EXCEPTIONS in the same pull request.
    assert sorted(set(KNOWN_EXCEPTIONS) - _found()) == []
