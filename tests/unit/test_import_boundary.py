"""Static import boundary between apps (#99): absolute, relative and from-imports."""

import ast
from pathlib import Path

import pytest

from skeleton.system.import_boundary import (
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
    ],
)
def test_public_core_surfaces_are_allowed(module):
    assert classify(module) is None


@pytest.mark.parametrize("module", ["skeleton.access.services", "skeleton.accounts.models", "skeleton.audit.models"])
def test_other_core_modules_are_warnings(module):
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
