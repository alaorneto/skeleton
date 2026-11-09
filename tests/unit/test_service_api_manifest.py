"""runtime.service_api in the manifest, package doctor/build and the E018 structure (#98)."""

import copy
import shutil
from pathlib import Path

import pytest

from skeleton.service_api import ServiceAPI, service_method
from skeleton.service_api.contract import structural_problems
from skeleton.system.packages import (
    SERVICE_API_CORE_MINIMUM,
    PackageError,
    build_package,
    canonical_json,
    default_service_class,
    doctor,
    load_manifest,
    manifest_template,
    validate_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
NOTES = ROOT / "examples" / "notes"


def template(**options):
    return manifest_template(
        app_id="org.test.demo", app_label="project_demo", package_name="demo", publisher="test-publisher", **options
    )


def with_service(manifest, **service):
    manifest = copy.deepcopy(manifest)
    manifest["runtime"]["service_api"] = {"module": "applications.demo.service_api", "classes": ["DemoService"]}
    manifest["runtime"]["service_api"].update(service)
    return manifest


def test_template_declares_service_api_with_the_core_floor():
    manifest = template(service_classes=["DemoService"])
    assert manifest["runtime"]["service_api"] == {"module": "applications.demo.service_api", "classes": ["DemoService"]}
    assert f">={SERVICE_API_CORE_MINIMUM}" in manifest["compatibility"]["skeleton"].split(",")
    assert validate_manifest(manifest) == manifest
    assert default_service_class("tasks_board") == "TasksBoardService"


def test_historical_manifest_without_service_api_still_loads():
    manifest = template()
    assert "service_api" not in manifest["runtime"]
    assert validate_manifest(manifest) == manifest


@pytest.mark.parametrize(
    "service",
    [
        {"module": "applications.other.service_api"},
        {"module": "skeleton.service_api"},
        {"module": "applications.demo"},
        {"module": "applications.demo.service_api; import os"},
        {"module": 7},
        {"classes": []},
        {"classes": ["DemoService", "DemoService"]},
        {"classes": ["demo_service"]},
        {"classes": ["applications.demo.Service"]},
        {"classes": "DemoService"},
        {"extra": True},
    ],
)
def test_service_api_is_resolved_only_inside_the_app_package(service):
    manifest = with_service(template(service_classes=["DemoService"]), **service)
    with pytest.raises(PackageError):
        validate_manifest(manifest)


def test_declaring_service_api_requires_the_core_floor():
    manifest = with_service(template())
    assert ">=0.3.0" in manifest["compatibility"]["skeleton"].split(",")
    with pytest.raises(PackageError, match="core minimum"):
        validate_manifest(manifest)
    manifest["compatibility"]["skeleton"] = f">={SERVICE_API_CORE_MINIMUM},<1.0.0"
    assert validate_manifest(manifest)["runtime"]["service_api"]["classes"] == ["DemoService"]


def test_manifest_v1_cannot_declare_service_api():
    manifest = with_service(template(service_classes=["DemoService"]))
    manifest["manifest_version"] = 1
    for key in ("administration", "model_ui"):
        manifest["runtime"].pop(key)
    with pytest.raises(PackageError):
        validate_manifest(manifest)


def notes_copy(tmp_path):
    target = tmp_path / "notes"
    shutil.copytree(NOTES, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def codes(result):
    return [problem["code"] for problem in result["problems"]]


def drop_service_api(root):
    manifest = load_manifest(root / "manifest.json")
    del manifest["runtime"]["service_api"]
    (root / "manifest.json").write_bytes(canonical_json(manifest))


def test_doctor_refuses_a_new_release_without_service_api(tmp_path):
    root = notes_copy(tmp_path)
    drop_service_api(root)
    result = doctor(root)
    assert not result["ok"]
    assert codes(result) == ["service_api_missing"]


def test_doctor_requires_the_declared_module_and_its_reference(tmp_path):
    root = notes_copy(tmp_path)
    (root / "service_api.py").unlink()
    (root / "docs" / "service-api.md").write_text("# Sem seções\n", encoding="utf-8")
    assert sorted(codes(doctor(root))) == ["documentation_sections", "service_api_module_missing"]
    (root / "docs" / "service-api.md").unlink()
    assert "documentation_missing" in codes(doctor(root))


def test_build_refuses_a_manifest_without_service_api(tmp_path):
    root = notes_copy(tmp_path)
    drop_service_api(root)
    with pytest.raises(PackageError, match="runtime.service_api"):
        build_package(root, root / "manifest.json", tmp_path / "out.skapp", tmp_path / "missing-key.pem")


class Valid(ServiceAPI):
    app_label = "project_demo"

    @service_method(read_only=True, since="1.0")
    def count(self, *, actor) -> int:
        """Counts.

        Autorização: read. Erros: nenhum. Efeitos: nenhum.
        """
        return 0

    def _helper(self, value):
        return value


class Undecorated(Valid):
    def total(self, value):
        return value


class Undocumented(ServiceAPI):
    @service_method(read_only=True, since="1.0")
    def count(self, *, actor) -> int:
        return 0


class NeedsArguments(Valid):
    def __init__(self, dependency):
        self.dependency = dependency


class Empty(ServiceAPI):
    pass


class StaticEntry(Valid):
    @staticmethod
    def shortcut(*, actor):
        return actor


class NotAService:
    def count(self, *, actor):
        return 0


def test_structure_of_a_valid_service_has_no_problems():
    assert structural_problems(Valid) == []


@pytest.mark.parametrize(
    ("service_class", "fragment"),
    [
        (Undecorated, "without @service_method"),
        (Undocumented, "docstring"),
        (NeedsArguments, "without arguments"),
        (Empty, "no @service_method"),
        (StaticEntry, "instance methods"),
        (NotAService, "derive from"),
        (object(), "derive from"),
    ],
)
def test_structural_problems_are_reported(service_class, fragment):
    problems = structural_problems(service_class)
    assert any(fragment in problem for problem in problems), problems


def test_positional_parameters_are_reported_even_if_the_decorator_was_bypassed():
    from skeleton.service_api.base import SPEC_ATTRIBUTE, ServiceMethodSpec

    def positional(self, actor):
        """Doc."""

    setattr(positional, SPEC_ATTRIBUTE, ServiceMethodSpec("positional", True, False, True, "1.0"))
    Bypassed = type("Bypassed", (ServiceAPI,), {"positional": positional})
    problems = structural_problems(Bypassed)
    assert any("keyword-only" in problem for problem in problems), problems
