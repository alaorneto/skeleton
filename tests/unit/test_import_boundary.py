"""Static import boundary between apps (#99): absolute, relative and from-imports."""

import ast
import re
from pathlib import Path

import pytest

from skeleton.system.import_boundary import (
    PUBLIC_CORE_NAMES,
    Provider,
    classify_import,
    imported_modules,
    providers_from_manifests,
    scan_application,
)

TASKS = Provider(
    identity="com.example.tasks",
    packages=("applications.tasks",),
    service_modules=("applications.tasks.service_api",),
)


def classify(module, candidate=None, *, dependencies=("com.example.tasks",)):
    result = classify_import(
        module,
        candidate or module,
        own_packages=("applications.billing",),
        providers=[TASKS],
        dependencies=dependencies,
    )
    return result[0] if result else None


def imports(source, module="applications.billing.views", *, is_package=False):
    return [
        (found, candidate) for _, found, candidate in imported_modules(ast.parse(source), module, is_package=is_package)
    ]


def test_absolute_relative_and_from_imports_are_resolved():
    assert imports("import applications.tasks.models") == [("applications.tasks.models", "applications.tasks.models")]
    assert imports("from applications.tasks import models") == [("applications.tasks", "applications.tasks.models")]
    assert imports("from . import forms") == [("applications.billing", "applications.billing.forms")]
    assert imports("from ..tasks.models import Task") == [
        ("applications.tasks.models", "applications.tasks.models.Task")
    ]
    assert imports("from .models import Invoice", "applications.billing", is_package=True) == [
        ("applications.billing.models", "applications.billing.models.Invoice")
    ]
    assert imports("from ....beyond import x") == []
    assert imports("from os import *") == [("os", "os")]


@pytest.mark.parametrize(
    "module",
    ["applications.tasks.models", "applications.tasks.managers", "applications.tasks.services", "applications.tasks"],
)
def test_internal_modules_of_another_app_are_errors(module):
    assert classify(module) == "internal_import"


def test_from_package_import_internal_submodule_is_an_error():
    assert classify("applications.tasks", "applications.tasks.models") == "internal_import"
    assert classify("applications", "applications.tasks") == "internal_import"


def test_declared_service_api_with_dependency_passes():
    assert classify("applications.tasks.service_api", "applications.tasks.service_api.TaskService") is None
    assert classify("applications.tasks", "applications.tasks.service_api") is None
    assert classify("applications.tasks.service_api.dto") is None


def test_service_api_without_declared_dependency_is_an_error():
    assert classify("applications.tasks.service_api", dependencies=()) == "undeclared_dependency"


def test_own_package_third_party_and_django_are_ignored():
    assert classify("applications.billing.models") is None
    assert classify("django.db.models") is None
    assert classify("rest_framework.response") is None


@pytest.mark.parametrize(
    "module",
    [
        "skeleton.service_api",
        "skeleton.service_api.errors",
        "skeleton.common.models",
        "skeleton.access.policies",
        "skeleton.tenancy.context",
        "skeleton.web.views",
        "skeleton.api.views",
        "skeleton.system.app_registry",
        "skeleton.system.applications",
        "skeleton.testing.service_api",
        "skeleton.sequences.registry",
    ],
)
def test_public_core_surfaces_are_allowed(module):
    assert classify(module) is None


@pytest.mark.parametrize("module", ["skeleton.access.services", "skeleton.accounts.models", "skeleton.audit.models"])
def test_other_core_modules_are_warnings(module):
    assert classify(module) == "core_import"


@pytest.mark.parametrize(
    "module",
    [
        "skeleton.sequences.services",
        "skeleton.events.core_events",
        "skeleton.events.services",
        "skeleton.jobs.services",
        "skeleton.notifications.services",
        "skeleton.common.exceptions",
    ],
)
def test_internal_subsystem_modules_stay_private(module):
    assert classify(module) == "core_import"


def test_from_core_package_import_public_submodule_is_allowed():
    assert classify("skeleton.common", "skeleton.common.models") is None
    assert classify("skeleton.access", "skeleton.access.services") == "core_import"


def test_unknown_applications_package_only_allows_its_service_api():
    assert classify("applications.crm.models") == "internal_import"
    assert classify("applications.crm.service_api") is None


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scan_reports_paths_lines_and_unreadable_sources(tmp_path):
    root = tmp_path / "billing"
    write(root, "__init__.py", "")
    write(root, "views.py", "import os\nfrom applications.tasks.models import Task\n")
    write(
        root, "jobs.py", "from applications.tasks.service_api import TaskService\nfrom skeleton.audit import models\n"
    )
    write(root, "broken.py", "def (:\n")
    write(root, "__pycache__/cached.py", "from applications.tasks.models import Task\n")
    violations = scan_application(
        root, package="applications.billing", providers=[TASKS], dependencies=["com.example.tasks"]
    )
    found = sorted((item.path, item.line, item.code) for item in violations)
    assert found == [
        ("broken.py", 0, "unreadable_source"),
        ("jobs.py", 2, "core_import"),
        ("views.py", 2, "internal_import"),
    ]
    assert [item.is_error for item in violations if item.code == "core_import"] == [False]


def test_alias_names_the_same_package():
    root = Path(__file__).resolve().parents[2] / "examples" / "notes"
    assert (
        scan_application(root, package="examples.notes", aliases=["applications.notes"], providers=[], dependencies=[])
        == []
    )


def test_providers_from_inventory_manifests():
    providers = providers_from_manifests(
        {
            "com.example.tasks": {
                "package_name": "tasks",
                "runtime": {"service_api": {"module": "applications.tasks.service_api", "classes": ["TaskService"]}},
            },
            "com.example.legacy": {"package_name": "legacy", "runtime": {}},
        }
    )
    assert providers == [
        Provider("com.example.legacy", ("applications.legacy",), ()),
        TASKS,
    ]


# Closed names, private names, get_model, directories and first-party providers (#300).


@pytest.mark.parametrize(
    "module,name",
    [
        ("skeleton.tenancy.context", "system_context"),
        ("skeleton.tenancy.context", "tenant_context"),
        ("skeleton.tenancy.context", "request_context"),
        ("skeleton.common.models", "OperationalRecord"),
        ("skeleton.web.views", "outro_nome"),
        ("skeleton.api.views", "outro_nome"),
    ],
)
def test_names_outside_the_public_list_are_warnings(module, name):
    result = classify_import(
        module, f"{module}.{name}", own_packages=("applications.billing",), providers=[], dependencies=[]
    )
    assert result is not None and result[0] == "core_import"
    assert name in result[1]


def test_listed_public_names_pass():
    for module, names in PUBLIC_CORE_NAMES.items():
        for name in sorted(names or {"anything"}):
            assert classify(module, f"{module}.{name}") is None, (module, name)


def test_private_core_names_are_warnings():
    assert classify("skeleton.access.policies", "skeleton.access.policies._grant_ids") == "core_import"
    assert classify("skeleton.service_api.base", "skeleton.service_api.base._qualquer") == "core_import"
    assert classify("skeleton.api.openapi", "skeleton.api.openapi._resolve") == "core_import"
    assert classify("skeleton.service_api._internal") == "core_import"


def test_plain_module_import_of_a_public_module_still_passes():
    assert classify("skeleton.tenancy.context") is None
    assert classify("skeleton.common", "skeleton.common.models") is None
    assert classify("skeleton.flows", "skeleton.flows.registry") is None


def test_get_model_with_core_label_is_a_warning_outside_migrations(tmp_path):
    root = tmp_path / "billing"
    lookups = (
        "from django.apps import apps\n"
        "A = apps.get_model('sk_tenancy', 'Membership')\n"
        "B = apps.get_model('sk_tenancy.Membership')\n"
        "C = apps.get_model('project_tasks', 'Task')\n"
    )
    write(root, "__init__.py", "")
    write(root, "services.py", lookups)
    write(root, "migrations/__init__.py", "")
    write(root, "migrations/0002_x.py", lookups)
    violations = scan_application(root, package="applications.billing", providers=[], dependencies=[])
    found = sorted((item.path, item.line, item.code, item.module) for item in violations)
    assert found == [
        ("services.py", 2, "core_import", "get_model('sk_tenancy')"),
        ("services.py", 3, "core_import", "get_model('sk_tenancy.Membership')"),
    ]
    assert not any(item.is_error for item in violations)


def test_build_dist_and_node_modules_sources_are_scanned(tmp_path):
    root = tmp_path / "billing"
    write(root, "__init__.py", "")
    for directory in ("build", "dist", "node_modules", "ok", "__pycache__", ".hidden"):
        write(root, f"{directory}/m.py", "from applications.tasks.models import Task\n")
    violations = scan_application(root, package="applications.billing", providers=[TASKS], dependencies=[])
    assert sorted(item.path for item in violations) == ["build/m.py", "dist/m.py", "node_modules/m.py", "ok/m.py"]
    assert {item.code for item in violations} == {"internal_import"}


CALENDAR = Provider(
    identity="org.skeleton.calendar",
    packages=("skeleton.contrib.calendar",),
    service_modules=("skeleton.contrib.calendar.service_api",),
)


def first_party(module, candidate, dependencies):
    result = classify_import(
        module, candidate, own_packages=("applications.mine",), providers=[CALENDAR], dependencies=dependencies
    )
    return result[0] if result else None


def test_first_party_service_api_follows_the_provider_rules():
    service = "skeleton.contrib.calendar.service_api"
    assert first_party(service, f"{service}.CalendarService", ["org.skeleton.calendar"]) is None
    assert first_party(service, f"{service}.CalendarService", []) == "undeclared_dependency"
    models = "skeleton.contrib.calendar.models"
    assert first_party(models, f"{models}.Event", ["org.skeleton.calendar"]) == "internal_import"


def test_unknown_first_party_package_only_allows_its_service_api():
    assert classify("skeleton.contrib.crm.models", "skeleton.contrib.crm.models.Lead") == "internal_import"
    assert classify("skeleton.contrib.crm.service_api", "skeleton.contrib.crm.service_api.CrmService") is None


def test_public_names_match_the_documented_table():
    """Every module of the closed list appears in the documented table, with each listed name (#300)."""
    document = (Path(__file__).resolve().parents[2] / "docs" / "service-api.md").read_text(encoding="utf-8")
    start = document.index("## Superfícies públicas do núcleo para apps")
    table = document[start : document.index("### Fronteira verificada", start)]
    rows = {}
    for line in table.splitlines():
        if line.startswith("| `skeleton."):
            cells = line.split("|")
            for module in re.findall(r"`(skeleton\.[a-z_.]+)`", cells[1]):
                rows[module] = rows.get(module, "") + cells[2]
    for module, names in PUBLIC_CORE_NAMES.items():
        assert module in rows, module
        for name in names or ():
            assert f"`{name}`" in rows[module], (module, name)
