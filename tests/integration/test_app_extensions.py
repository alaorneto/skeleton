"""Behavioral acceptance for EA01, EA03–EA09."""

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import resolve

from skeleton.accounts.services import bootstrap_identity
from skeleton.tenancy.context import tenant_context
from skeleton.testing.applications import activate_app, activation_revision, open_app

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "App-extension-test-password!49"


@pytest.fixture
def app_actor():
    actor, organization = bootstrap_identity(
        email="apps-owner@example.test", organization="apps-test", name="Apps", password=PASSWORD
    )
    client = Client()
    assert client.post("/accounts/login/", {"login": actor.email, "password": PASSWORD}).status_code == 302
    return actor, organization, client


def test_ea01_activation_is_tenant_scoped_cas_and_audited(app_actor):
    from skeleton.audit.models import AuditEvent
    from skeleton.common.exceptions import ConcurrentUpdate
    from skeleton.system.applications import application_state, configure_application

    actor, organization, _ = app_actor
    _, other = bootstrap_identity(email=actor.email, organization="apps-other", name="Other")
    with tenant_context(organization_id=organization.pk, actor=actor, source="test:apps"):
        before = application_state("example_notes")
        changed = configure_application(
            "example_notes", user=actor, enabled=False, configuration={}, revision=before["revision"]
        )
        assert not application_state("example_notes")["enabled"]
        assert AuditEvent.objects.filter(record_sysid=changed.pk, operation="create").exists()
        with pytest.raises(ConcurrentUpdate):
            configure_application(
                "example_notes", user=actor, enabled=True, configuration={}, revision=before["revision"]
            )
    with tenant_context(organization_id=other.pk, actor=actor, source="test:apps-other"):
        assert application_state("example_notes")["enabled"]


def test_ea03_extension_routes_precede_generic_resource_capture(app_actor):
    actor, organization, client = app_actor
    path = f"/api/v1/o/{organization.pk}/apps/example_notes/summary/"
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {"count": 0, "heading": "Anotações"}
    assert resolve(path).namespace == "example_notes_api"


def test_ea04_extension_routes_require_validated_tenant(app_actor):
    actor, organization, client = app_actor
    _, foreign = bootstrap_identity(
        email="foreign-owner@example.test", organization="foreign-apps", name="Foreign", password=PASSWORD
    )
    assert client.get(f"/api/v1/o/{organization.pk}/apps/example_notes/summary/").status_code == 200
    assert client.get(f"/api/v1/o/{foreign.pk}/apps/example_notes/summary/").status_code == 404
    assert Client().get(f"/api/v1/o/{organization.pk}/apps/example_notes/summary/").status_code == 403


def test_ea05_navigation_hides_disabled_or_unauthorized_apps(app_actor):
    from skeleton.system.applications import configure_application, navigation_for_user

    actor, organization, client = app_actor
    with tenant_context(organization_id=organization.pk, actor=actor, source="test:menu"):
        links = navigation_for_user(actor)
        assert any(link["app_label"] == "example_notes" for link in links)
        configure_application(
            "example_notes", user=actor, enabled=False, configuration={}, revision=activation_revision("example_notes")
        )
        assert not any(link["app_label"] == "example_notes" for link in navigation_for_user(actor))
    response = client.get(f"/o/{organization.pk}/")
    assert response.status_code == 200
    assert f"/o/{organization.pk}/apps/example_notes/".encode() not in response.content
    assert client.get(f"/api/v1/o/{organization.pk}/tables/example_notes.note/records/").status_code == 403


def test_ea06_manifest_dependency_and_configuration_contract(app_actor):
    from skeleton.system.app_registry import AppManifest, register_app, unregister_app
    from skeleton.system.applications import application_state, configure_application

    actor, organization, client = app_actor
    register_app(
        AppManifest(
            app_label="dependent_fixture",
            label="Dependent",
            description="Fixture",
            dependencies=("example_notes",),
            configuration_fields=(),
        )
    )
    activate_app("dependent_fixture", organization.pk)  # An enabled dependent blocks disabling Notes.
    try:
        with tenant_context(organization_id=organization.pk, actor=actor, source="test:dependencies"):
            with pytest.raises(ValidationError):
                configure_application(
                    "example_notes",
                    user=actor,
                    enabled=False,
                    configuration={},
                    revision=activation_revision("example_notes"),
                )
            with pytest.raises(ValidationError):
                configure_application(
                    "example_notes",
                    user=actor,
                    enabled=True,
                    configuration={"unknown": "secret"},
                    revision=activation_revision("example_notes"),
                )
            configure_application(
                "example_notes",
                user=actor,
                enabled=True,
                configuration={"heading": "Team notes"},
                revision=activation_revision("example_notes"),
            )
            assert application_state("example_notes")["configuration"]["heading"] == "Team notes"
    finally:
        unregister_app("dependent_fixture")


def test_ea07_explicit_external_application_rejects_non_uuid_model():
    from types import ModuleType

    from django.apps import AppConfig
    from django.db import models
    from django.test.utils import isolate_apps

    from skeleton.system.app_registry import AppManifest, register_app, unregister_app
    from skeleton.system.checks import record_contract_checks

    with isolate_apps() as registry:
        module = ModuleType("outside_business")
        module.__file__ = __file__
        config = AppConfig("outside_business", module)
        config.label = "external_inventory"
        config.apps = registry
        config.models = registry.all_models[config.label]
        registry.app_configs[config.label] = config

        class ExternalEntity(models.Model):
            class Meta:
                app_label = "external_inventory"

        register_app(AppManifest(app_label="external_inventory", label="External fixture", description="Fixture"))
        try:
            errors = record_contract_checks(apps_registry=registry)
            assert any(error.id == "skeleton.E001" and "ExternalEntity" in error.msg for error in errors)
        finally:
            unregister_app("external_inventory")


def test_ea08_example_configuration_changes_its_own_domain_surface(app_actor):
    from skeleton.system.applications import configure_application

    actor, organization, client = app_actor
    with tenant_context(organization_id=organization.pk, actor=actor, source="test:example"):
        configure_application(
            "example_notes",
            user=actor,
            enabled=True,
            configuration={"heading": "My tenant heading"},
            revision=activation_revision("example_notes"),
        )
    response = client.get(f"/o/{organization.pk}/apps/example_notes/")
    assert response.status_code == 200
    assert b"My tenant heading" in response.content
    assert (
        client.get(f"/api/v1/o/{organization.pk}/apps/example_notes/summary/").json()["heading"] == "My tenant heading"
    )


def test_ea09_manifest_requires_spec_and_decision_documents(tmp_path):
    from skeleton.system.app_registry import AppManifest, validate_documentation

    manifest = AppManifest(
        app_label="documentation_fixture", label="Docs", description="Fixture", documentation_root=str(tmp_path)
    )
    with pytest.raises(ValidationError):
        validate_documentation(manifest)
    (tmp_path / "spec.md").write_text(
        "# Specification\n\n## Contract\nTenant-scoped.\n\n## Acceptance\nNo cross-tenant data.\n"
    )
    (tmp_path / "adr.md").write_text(
        "# Decision\n\n## Context\nExtension.\n\n## Decision\nUse secured records.\n\n## Consequences\nDefault deny.\n"
    )
    validate_documentation(manifest)


def test_activation_follows_transitive_dependencies_and_names_blockers(app_actor):
    from django.core.exceptions import PermissionDenied

    from skeleton.system.app_registry import AppManifest, register_app, unregister_app
    from skeleton.system.applications import configure_application, require_application_enabled

    actor, organization, client = app_actor
    for label, dependencies in (("middle_fixture", ("example_notes",)), ("top_fixture", ("middle_fixture",))):
        register_app(
            AppManifest(app_label=label, label=label.title(), description="Fixture", dependencies=dependencies)
        )
        open_app(label)
        activate_app(label, organization.pk)
    try:
        with tenant_context(organization_id=organization.pk, actor=actor, source="test:transitive"):
            with pytest.raises(ValidationError, match="Middle_Fixture, Top_Fixture"):
                configure_application(
                    "example_notes",
                    user=actor,
                    enabled=False,
                    configuration={},
                    revision=activation_revision("example_notes"),
                )
            configure_application(
                "top_fixture", user=actor, enabled=False, configuration={}, revision=activation_revision("top_fixture")
            )
            configure_application(
                "middle_fixture",
                user=actor,
                enabled=False,
                configuration={},
                revision=activation_revision("middle_fixture"),
            )
            configure_application(
                "example_notes",
                user=actor,
                enabled=False,
                configuration={},
                revision=activation_revision("example_notes"),
            )
            with pytest.raises(ValidationError, match="Enable required applications first: Anotações, Middle_Fixture"):
                configure_application(
                    "top_fixture",
                    user=actor,
                    enabled=True,
                    configuration={},
                    revision=activation_revision("top_fixture"),
                )
            with pytest.raises(PermissionDenied):
                require_application_enabled("top_fixture")
    finally:
        unregister_app("top_fixture")
        unregister_app("middle_fixture")


def test_dependency_check_reports_each_missing_and_cyclic_application():
    from skeleton.system.app_registry import AppManifest, register_app, unregister_app
    from skeleton.system.checks import record_contract_checks

    fixtures = {"loop_a": ("loop_b",), "loop_b": ("loop_a",), "orphan_fixture": ("absent_fixture",)}
    for label, dependencies in fixtures.items():
        register_app(AppManifest(app_label=label, label=label, description="Fixture", dependencies=dependencies))
    try:
        messages = [error.msg for error in record_contract_checks() if error.id == "skeleton.E011"]
        assert any("missing_dependency: orphan_fixture -> absent_fixture" in message for message in messages)
        assert sum("dependency_cycle" in message for message in messages) == 1
    finally:
        for label in fixtures:
            unregister_app(label)
