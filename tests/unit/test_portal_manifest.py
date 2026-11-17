"""The portal routes in the manifest (#533): owned modules, the core floor and the reserved root."""

import json

import pytest

from skeleton.system.packages import PackageError, canonical_json, manifest_template, validate_manifest
from skeleton.versioning import version_satisfies


@pytest.fixture
def manifest():
    return manifest_template(
        app_id="org.test.demo",
        app_label="project_demo",
        package_name="package_demo",
        publisher="test-publisher",
        app_config_class="DemoConfig",
        service_classes=["DemoService"],
    )


def test_portal_modules_must_belong_to_the_app_and_need_the_portal_floor(manifest):
    manifest["runtime"]["portal_urls"] = "applications.package_demo.portal_urls"
    manifest["compatibility"]["skeleton"] = ">=0.4.0,<1.0.0"
    with pytest.raises(PackageError, match="portal_urls requires a core minimum of 0.6.0"):
        validate_manifest(json.loads(canonical_json(manifest)))
    manifest["compatibility"]["skeleton"] = ">=0.6.0,<1.0.0"
    assert validate_manifest(json.loads(canonical_json(manifest)))["runtime"]["portal_urls"].endswith("portal_urls")
    for key in ("portal_urls", "portal_api_urls"):
        foreign = json.loads(canonical_json(manifest))
        foreign["runtime"][key] = "applications.other_app.portal_urls"
        with pytest.raises(PackageError, match="must belong to the application"):
            validate_manifest(foreign)


@pytest.mark.posix
def test_the_build_raises_the_floor_of_an_app_with_a_portal(tmp_path):
    from skeleton.system.packages import build_package, initialize_keys, validate_package

    root = tmp_path / "source"
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "apps.py").write_text(
        'from django.apps import AppConfig\nclass DemoConfig(AppConfig):\n    name = __package__\n    label = "project_demo"\n'
    )
    (root / "portal_urls.py").write_text("urlpatterns = []\n")
    (root / "docs").mkdir()
    (root / "docs/spec.md").write_text("## Contract\nPortal.\n## Acceptance\nPortal.\n")
    (root / "docs/adr.md").write_text("## Context\nPortal.\n## Decision\nPortal.\n## Consequences\nPortal.\n")
    (root / "service_api.py").write_text("class DemoService:\n    pass\n")
    (root / "docs/service-api.md").write_text(
        "## Serviços\nDemoService.\n## DTOs\nNenhum.\n## Histórico de versões\n0.1.0.\n", encoding="utf-8"
    )
    manifest = manifest_template(
        app_id="org.test.demo",
        app_label="project_demo",
        package_name="package_demo",
        publisher="test-publisher",
        app_config_class="DemoConfig",
        service_classes=["DemoService"],
    )
    manifest["runtime"]["portal_urls"] = "applications.package_demo.portal_urls"
    manifest["compatibility"]["skeleton"] = ">=0.6.0,<1.0.0"
    path = root / "manifest.json"
    path.write_bytes(canonical_json(manifest))
    private, public = initialize_keys(tmp_path / "private.pem", tmp_path / "public.pem")
    package = build_package(root, path, tmp_path / "demo.skapp", private)
    published = validate_package(package, {"test-publisher": public}).manifest
    assert version_satisfies("0.6.0", published["compatibility"]["skeleton"])
    assert not version_satisfies("0.5.9", published["compatibility"]["skeleton"])


def test_portal_is_a_reserved_root():
    from django.core.exceptions import ValidationError
    from django.urls import path

    from skeleton.system.presentation import RESERVED_ROOTS, _validate_reserved_routes

    assert "portal" in RESERVED_ROOTS
    for route in ("portal/", "portal/<uuid:x>/", "<slug:root>/"):
        with pytest.raises(ValidationError):
            _validate_reserved_routes([path(route, lambda request: None)])
