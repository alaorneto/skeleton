"""First-party libraries (#792): manifest rules and the effective state derived from the apps."""

from contextlib import contextmanager

import pytest
from test_bundled_apps import bundled_manifest

from skeleton.system import app_registry
from skeleton.system.app_registry import AppManifest
from skeleton.system.packages import PackageError, validate_manifest


def library_manifest(**runtime):
    manifest = bundled_manifest(kind="bundled_library")
    manifest["runtime"].update({"default_enabled": False, "configuration_fields": [], **runtime})
    return manifest


def test_a_library_manifest_is_a_first_party_manifest():
    validated = validate_manifest(library_manifest())
    assert validated["kind"] == "bundled_library"
    third = library_manifest()
    third.update(app_label="agenda", migrations={"leaves": [], "from_versions": ">=0.0.0", "labels": ["agenda"]})
    with pytest.raises(PackageError, match="reserved"):  # The skeleton_ prefix and contrib namespace still apply.
        validate_manifest(third)


@pytest.mark.parametrize(
    "runtime",
    [
        {"default_enabled": True},
        {"configuration_fields": [{"name": "colour", "type": "str", "default": "blue"}]},
    ],
    ids=["default-enabled", "configuration"],
)
def test_a_library_has_no_activation_nor_configuration(runtime):
    with pytest.raises(PackageError, match="library"):
        validate_manifest(library_manifest(**runtime))


def test_the_shipped_calendar_and_ledger_are_libraries():
    import json
    from pathlib import Path

    root = Path(app_registry.__file__).resolve().parents[1] / "contrib"
    for name in ("calendar", "ledger"):
        manifest = json.loads((root / name / "manifest.json").read_text(encoding="utf-8"))
        assert validate_manifest(manifest)["kind"] == "bundled_library"


@contextmanager
def registered(*manifests):
    saved = dict(app_registry._manifests)
    app_registry._manifests.clear()
    try:
        for manifest in manifests:
            app_registry._manifests[manifest.app_label] = manifest
        yield
    finally:
        app_registry._manifests.clear()
        app_registry._manifests.update(saved)


def app(label, *dependencies, library=False):
    return AppManifest(
        app_label=label, label=label, description="", dependencies=dependencies, library=library, bundled=library
    )


GRAPH = (
    app("lib_base", library=True),
    app("lib_top", "lib_base", library=True),
    app("first", "lib_top"),
    app("second", "lib_base"),
    app("third", "first"),
    app("unused_lib", library=True),
)


def test_a_library_is_effective_while_an_effective_app_requires_it():
    with registered(*GRAPH):
        nothing = app_registry.effective_map({})
        assert not any(nothing.values())
        only_first = app_registry.effective_map({"first": True})
        assert only_first["first"] and only_first["lib_top"] and only_first["lib_base"]  # Transitively.
        assert not only_first["second"] and not only_first["unused_lib"]
        assert app_registry.effective_map({"second": True})["lib_base"]
        assert not app_registry.effective_map({"second": True})["lib_top"]
        # An app whose required app is closed is not effective, and neither are its libraries.
        third = app_registry.effective_map({"third": True})
        assert not third["third"] and not third["lib_top"]
        assert app_registry.effective_map({"third": True, "first": True})["third"]


def test_the_app_closure_without_libraries():
    with registered(*GRAPH):
        assert app_registry.required_app_labels("third") == ["first"]
        assert app_registry.dependent_app_labels("lib_base") == ["first", "second", "third"]
        assert app_registry.library_labels() == ("lib_base", "lib_top", "unused_lib")
        assert app_registry.is_library("lib_top") and not app_registry.is_library("first")
        assert not app_registry.is_library("missing")


def test_a_library_never_requires_an_app():
    with registered(app("lib_wrong", "first", library=True), app("first")):
        assert "library_dependency: lib_wrong -> first" in app_registry.dependency_problems()
    with registered(*GRAPH):
        assert not [problem for problem in app_registry.dependency_problems() if "library" in problem]


def test_refusals_carry_the_library_code():
    from django.core.exceptions import ValidationError

    with registered(*GRAPH):
        app_registry.require_not_library("first")
        with pytest.raises(ValidationError) as refused:
            app_registry.require_not_library("lib_top", field="enabled")
    assert refused.value.error_dict["enabled"][0].code == "library"
