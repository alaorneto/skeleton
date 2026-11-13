"""skeleton_startapp: generated app files, manifest, E012 docs and refusals."""

import io
import tomllib
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from skeleton.system.app_registry import validate_documentation
from skeleton.system.app_scaffold import TEMPLATE_ROOT
from skeleton.system.packages import load_manifest, validate_manifest

EXPECTED_FILES = {
    "__init__.py",
    "apps.py",
    "models.py",
    "service_api.py",
    "views.py",
    "urls.py",
    "api_urls.py",
    "templates/project_tasks/home.html",
    "docs/spec.md",
    "docs/adr.md",
    "docs/service-api.md",
    "manifest.json",
    "migrations/__init__.py",
    "tests/__init__.py",
    "tests/conftest.py",
    "tests/test_service_api.py",
}
VALID = {"app_id": "com.example.tasks", "publisher": "example-publisher"}


def startapp(tmp_path, label="project_tasks", **options):
    stdout = io.StringIO()
    call_command("skeleton_startapp", label, directory=str(tmp_path), stdout=stdout, **{**VALID, **options})
    return stdout.getvalue()


def test_generates_complete_application(tmp_path):
    output = startapp(tmp_path, model="TaskItem", display_name="Tarefas")
    root = tmp_path / "project_tasks"
    generated = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert generated == EXPECTED_FILES

    manifest = load_manifest(root / "manifest.json")
    assert validate_manifest(manifest) == manifest
    assert manifest["app_label"] == "project_tasks"
    assert manifest["app_config"] == "applications.project_tasks.apps.ProjectTasksConfig"
    assert manifest["runtime"]["urls"] == "applications.project_tasks.urls"
    assert manifest["runtime"]["api_urls"] == "applications.project_tasks.api_urls"
    assert manifest["runtime"]["navigation"] == [
        {"label": "Tarefas", "url_name": "project_tasks:home", "table_key": "project_tasks.taskitem", "action": "read"}
    ]
    assert manifest["migrations"]["labels"] == ["project_tasks"]
    assert manifest["runtime"]["service_api"] == {
        "module": "applications.project_tasks.service_api",
        "classes": ["TaskItemService"],
    }
    assert ">=0.4.0" in manifest["compatibility"]["skeleton"].split(",")

    validate_documentation(SimpleNamespace(documentation_root=str(root / "docs"), service_api=object()))

    for path in root.rglob("*.py"):
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    apps_source = (root / "apps.py").read_text(encoding="utf-8")
    assert "class ProjectTasksConfig(AppConfig):" in apps_source
    assert 'label = "project_tasks"' in apps_source
    for allowlist in ("read_fields=(", "write_fields=(", "sensitive_fields=("):
        assert allowlist in apps_source
    assert "__all__" not in apps_source
    assert "class TaskItem(SecuredRecord):" in (root / "models.py").read_text(encoding="utf-8")
    service_source = (root / "service_api.py").read_text(encoding="utf-8")
    assert "class TaskItemService(ServiceAPI):" in service_source
    assert "app_label = APP_LABEL" in service_source and 'APP_LABEL = "project_tasks"' in service_source
    # The scenario tests of every generated method run on the distributed isolated database (#334).
    tests_source = (root / "tests" / "test_service_api.py").read_text(encoding="utf-8")
    for name in (
        "test_authorized_member_reads_own_organization",
        "test_default_deny_without_table_permission",
        "test_private_record_of_someone_else_is_not_found",
        "test_organizations_are_isolated",
        "test_disabled_application_is_unavailable",
        "test_actor_must_match_the_context",
    ):
        assert f"def {name}(scenario)" in tests_source
    assert "TaskItemService().taskitem_count(actor=actor)" in tests_source
    assert "from skeleton.testing.database import skeleton_db" in (root / "tests" / "conftest.py").read_text(
        encoding="utf-8"
    )
    views_source = (root / "views.py").read_text(encoding="utf-8")
    assert "TaskItemService().taskitem_count(actor=request.user)" in views_source
    assert "authorized_queryset" not in views_source and ".models import" not in views_source
    assert "assert_service_contract(TaskItemService)" in (root / "tests/test_service_api.py").read_text(
        encoding="utf-8"
    )
    assert "'project_tasks.taskitem'" in (root / "templates/project_tasks/home.html").read_text(encoding="utf-8")
    assert "$" not in "".join(path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file())

    for step in (
        "PROJECT_APPS",
        "makemigrations project_tasks",
        "record_infrastructure project_tasks --write",
        "migrate",
        "sync_table_catalog",
        "check_skeleton_schema",
        "package build",
    ):
        assert step in output


def test_package_name_and_default_model(tmp_path):
    startapp(tmp_path, package_name="tasks")
    root = tmp_path / "tasks"
    manifest = load_manifest(root / "manifest.json")
    assert manifest["package_name"] == "tasks"
    assert manifest["app_config"] == "applications.tasks.apps.TasksConfig"
    assert manifest["runtime"]["urls"] == "applications.tasks.urls"
    assert "class Item(SecuredRecord):" in (root / "models.py").read_text(encoding="utf-8")


def test_refuses_existing_directory_without_touching_it(tmp_path):
    existing = tmp_path / "project_tasks"
    existing.mkdir()
    (existing / "keep.txt").write_text("original", encoding="utf-8")
    with pytest.raises(CommandError, match="already exists"):
        startapp(tmp_path)
    assert [path.name for path in existing.iterdir()] == ["keep.txt"]
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(
    ("label", "options"),
    [
        ("Project-Tasks", {}),
        ("sk_tasks", {}),
        ("skeleton", {}),
        ("project_tasks", {"package_name": "Bad.Package"}),
        ("project_tasks", {"package_name": "config"}),
        ("project_tasks", {"app_id": "X"}),
        ("project_tasks", {"publisher": "bad publisher"}),
        ("project_tasks", {"model": "item"}),
        ("project_tasks", {"model": "Item; import os"}),
        ("project_tasks", {"display_name": 'Tarefas" + __import__("os")'}),
        ("project_tasks", {"display_name": "Linha\nquebrada"}),
    ],
)
def test_rejects_invalid_arguments_without_writing(tmp_path, label, options):
    with pytest.raises(CommandError):
        startapp(tmp_path, label=label, **options)
    assert list(tmp_path.iterdir()) == []


def test_templates_are_declared_as_package_data():
    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"]["skeleton.system"]
    package_root = TEMPLATE_ROOT.parent
    templates = [path for path in TEMPLATE_ROOT.iterdir() if path.is_file()]
    assert templates
    for path in templates:
        relative = path.relative_to(package_root)
        assert any(fnmatch(relative.as_posix(), pattern) for pattern in patterns), relative
