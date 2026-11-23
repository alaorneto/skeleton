"""Settings of an organization locked by the platform (#701).

PostgreSQL with the runtime role. A lock writes the value to the organization and refuses any
change by its people (page, REST API, service API) with ``setting_locked``; infrastructure and
the platform are not restricted; A's lock never reaches B.
"""

import json
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.service_api.errors import Conflict
from skeleton.service_api.organizations import OrganizationService
from skeleton.tenancy import admission
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.tenancy.setting_locks import (
    lock_setting,
    lockable_settings,
    parse_form_value,
    register_setting_provider,
    unlock_setting,
)
from skeleton.testing.service_api import acting_as, assert_service_contract

pytestmark = [pytest.mark.usefixtures("skeleton_db")]
PASSWORD = "Setting-locks-Password!701"
NOTES = "example_notes"


def demote(user):
    with system_context(reason="test:locks-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def setup():
    platform, org_a = bootstrap_identity(
        email="locks-platform@example.test", organization="locks-a", name="A", password=PASSWORD
    )
    admin_b, org_b = bootstrap_identity(
        email="locks-b@example.test", organization="locks-b", name="B", password=PASSWORD
    )
    demote(admin_b)
    admin_b.refresh_from_db()
    return platform, org_a, admin_b, org_b


def as_platform(user, fn):
    with request_context(actor=user, request_id=uuid4(), source="test:locks-platform"):
        return fn()


def lock(platform, organization, key, value, reason="Contrato", revision=0):
    return as_platform(
        platform,
        lambda: lock_setting(
            actor=platform, organization=organization, key=key, value=value, reason=reason, expected_revision=revision
        ),
    )


def settings_of(organization):
    from skeleton.tenancy.organization_settings import current_settings

    with system_context(reason="test:locks-read", organization_id=organization.pk):
        return current_settings()


def update(owner, organization, **values):
    from skeleton.tenancy.organization_settings import current_settings, revision_of, update_settings

    with tenant_context(organization_id=organization.pk, actor=owner, source="test:locks-update"):
        record = current_settings()
        data = {
            "display_name": getattr(record, "display_name", ""),
            "default_language": getattr(record, "default_language", ""),
            "default_timezone": getattr(record, "default_timezone", ""),
            "brand_color": getattr(record, "brand_color", ""),
            **values,
        }
        return update_settings(actor=owner, data=data, expected_revision=revision_of(record))


def test_registry_and_form_values():
    settings = lockable_settings()
    assert {"organization.brand_color", "organization.logo", "admission.discoverable"} <= set(settings)
    assert settings["organization.logo"].kind == "frozen"
    assert parse_form_value(settings["admission.discoverable"], "sim") is True
    assert parse_form_value(settings["admission.discoverable"], "não") is False
    with pytest.raises(ValidationError):
        parse_form_value(settings["admission.discoverable"], "talvez")
    with pytest.raises(ValueError):
        register_setting_provider("organization", lambda: ())  # Another provider for the same prefix.
    with pytest.raises(ValidationError):
        settings["organization.brand_color"].normalize("azul")


def test_locked_color_is_written_and_kept(setup):
    from skeleton.audit.models import AuditEvent

    platform, org_a, admin_b, org_b = setup
    row = lock(platform, org_a, "organization.brand_color", "#123456")
    assert settings_of(org_a).brand_color == "#123456"
    with pytest.raises(ValidationError) as refused:
        update(platform, org_a, brand_color="#654321")
    assert refused.value.error_dict["brand_color"][0].code == "setting_locked"
    assert settings_of(org_a).brand_color == "#123456"
    update(platform, org_a, display_name="Nome novo")  # Another field still changes.
    assert settings_of(org_a).display_name == "Nome novo"
    update(admin_b, org_b, brand_color="#654321")  # B is not locked.
    # The service API: Conflict, and the locked fields in the settings.
    with acting_as(platform, org_a) as actor:
        assert OrganizationService().settings(actor=actor).locked_fields == ("brand_color",)
        current = OrganizationService().settings(actor=actor)
        with pytest.raises(Conflict) as conflict:
            OrganizationService().update_settings(
                actor=actor,
                display_name=current.display_name,
                default_language="",
                default_timezone="",
                brand_color="#000000",
                expected_revision=current.revision,
            )
        assert conflict.value.code == "setting_locked"
    assert_service_contract(OrganizationService)
    # Unlock and lock again: the same row, three audited operations.
    as_platform(
        platform,
        lambda: unlock_setting(
            actor=platform, organization=org_a, key="organization.brand_color", expected_revision=row.revision
        ),
    )
    update(platform, org_a, brand_color="#654321")
    again = lock(platform, org_a, "organization.brand_color", "#123456")
    assert again.pk == row.pk and settings_of(org_a).brand_color == "#123456"
    operations = as_platform(
        platform,
        lambda: list(
            AuditEvent.objects.filter(record_sysid=row.pk, organization_id__isnull=True)
            .order_by("record_revision")
            .values_list("operation", flat=True)
        ),
    )
    assert operations == ["create", "soft_delete", "restore"]


def test_invalid_locks_write_nothing(setup):
    from skeleton.tenancy.models import OrganizationSettingLock

    platform, org_a, admin_b, org_b = setup
    for key, value, reason in (
        ("organization.unknown", "x", "Motivo"),
        ("organization.brand_color", "azul", "Motivo"),
        ("organization.brand_color", "#123456", "  "),
        ("admission.discoverable", "sim", "Motivo"),  # Not a bool: the form parses, the service does not.
    ):
        with pytest.raises(ValidationError):
            lock(platform, org_a, key, value, reason)
    row = lock(platform, org_a, "admission.discoverable", True)
    with pytest.raises(ConcurrentUpdate):
        lock(platform, org_a, "admission.discoverable", False, revision=0)
    with request_context(actor=admin_b, request_id=uuid4(), source="test:locks-denied"):
        with pytest.raises(PermissionDenied):
            lock_setting(actor=admin_b, organization=org_b, key="admission.discoverable", value=True, reason="x")
    with system_context(reason="test:locks-count"):
        assert list(OrganizationSettingLock.objects.values_list("pk", flat=True)) == [row.pk]


def test_admission_logo_and_infrastructure(setup):
    from skeleton.tenancy.branding import remove_logo

    platform, org_a, _, _ = setup
    lock(platform, org_a, "admission.join_requests_enabled", True)
    with tenant_context(organization_id=org_a.pk, actor=platform, source="test:locks-policy"):
        policy = admission.current_policy()
        assert policy.join_requests_enabled
        with pytest.raises(ValidationError) as refused:
            admission.configure_policy(
                actor=platform, data={"join_requests_enabled": False}, expected_revision=policy.revision
            )
        assert refused.value.error_dict["join_requests_enabled"][0].code == "setting_locked"
        admission.configure_policy(
            actor=platform,
            data={"join_requests_enabled": True, "discoverable": True},
            expected_revision=policy.revision,
        )
    lock(platform, org_a, "organization.logo", None)
    with tenant_context(organization_id=org_a.pk, actor=platform, source="test:locks-logo"):
        with pytest.raises(ValidationError) as frozen:
            remove_logo(actor=platform, expected_revision=0)
    assert frozen.value.error_dict["logo"][0].code == "setting_locked"
    # Infrastructure (system context) is never restricted.
    from skeleton.tenancy.admission import write_policy_flag

    with system_context(reason="test:locks-infrastructure", organization_id=org_a.pk):
        write_policy_flag("join_requests_enabled", False)


@pytest.mark.needs_example_app
@pytest.mark.closed_app_defaults
def test_locked_app_activation_and_field(setup, monkeypatch):
    from dataclasses import replace

    from skeleton.service_api.applications import ApplicationAdministrationService
    from skeleton.system import app_registry, availability
    from skeleton.system.app_registry import ConfigField

    platform, org_a, _, _ = setup
    notes = app_registry.get_app(NOTES)
    fields = (ConfigField("greeting", str, "Olá"), ConfigField("limit", int, 10))
    monkeypatch.setitem(app_registry._manifests, NOTES, replace(notes, configuration_fields=fields))
    as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="all", revision=0))
    assert {f"apps.{NOTES}.enabled", f"apps.{NOTES}.greeting"} <= set(lockable_settings())
    lock(platform, org_a, f"apps.{NOTES}.enabled", True)
    lock(platform, org_a, f"apps.{NOTES}.greeting", "Bem-vindo")
    service = ApplicationAdministrationService()
    with acting_as(platform, org_a) as actor:
        state = service.application(actor=actor, app_label=NOTES)
        assert state.enabled and dict((i.name, i.value) for i in state.configuration)["greeting"] == "Bem-vindo"
        assert state.locked_fields == ("enabled", "greeting")
        with pytest.raises(Conflict) as off:
            service.configure(
                actor=actor,
                app_label=NOTES,
                enabled=False,
                configuration=(("greeting", "Bem-vindo"), ("limit", 10)),
                expected_revision=state.revision,
            )
        assert off.value.code == "setting_locked"
        changed = service.configure(  # Another field stays editable.
            actor=actor,
            app_label=NOTES,
            enabled=True,
            configuration=(("greeting", "Bem-vindo"), ("limit", 20)),
            expected_revision=state.revision,
        )
        assert dict((i.name, i.value) for i in changed.configuration)["limit"] == 20
    # The platform can still turn it off (its own lock path), and so can infrastructure.
    lock(platform, org_a, f"apps.{NOTES}.enabled", False, revision=1)
    with acting_as(platform, org_a) as actor:
        assert not service.application(actor=actor, app_label=NOTES).enabled


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def test_pages_of_the_platform_and_of_the_organization(setup):
    platform, org_a, admin_b, _ = setup
    client = login(platform)
    url = reverse("platform:organization-settings", kwargs={"sysid": org_a.pk})

    def post(data, who=client, target=url):
        return who.post(target, {**data, "csrfmiddlewaretoken": who.cookies["csrftoken"].value})

    page = client.get(url)
    assert page.status_code == 200
    keys = {row["key"] for group in page.context["groups"] for row in group["rows"]}
    assert "organization.brand_color" in keys
    assert (
        post({"key": "organization.brand_color", "value": "#123456", "reason": "", "expected_revision": 0}).status_code
        == 400
    )
    assert (
        post(
            {"key": "organization.brand_color", "value": "#123456", "reason": "Marca", "expected_revision": 0}
        ).status_code
        == 302
    )
    assert settings_of(org_a).brand_color == "#123456"
    # The organization's page shows the field disabled and a forged POST is refused.
    settings_url = reverse("web:organization-settings", kwargs={"organization_id": org_a.pk})
    shown = client.get(settings_url)
    assert shown.context["form"].fields["brand_color"].widget.attrs.get("disabled")
    assert "Definido pela plataforma" in shown.content.decode()
    record = settings_of(org_a)
    base = {
        "display_name": "Outra",
        "default_language": "",
        "default_timezone": "",
        "business_hours": "",
        "holidays": "",
        "revision": record.revision,
    }
    assert post(base, target=settings_url).status_code == 302  # Without the disabled field: the locked value.
    assert settings_of(org_a).brand_color == "#123456" and settings_of(org_a).display_name == "Outra"
    forged = post({**base, "brand_color": "#654321", "revision": settings_of(org_a).revision}, target=settings_url)
    assert forged.status_code == 409 and settings_of(org_a).brand_color == "#123456"
    # Unlock from the platform.
    lock_row = next(
        row["lock"]
        for group in client.get(url).context["groups"]
        for row in group["rows"]
        if row["key"] == "organization.brand_color"
    )
    assert (
        post(
            {"action": "unlock", "key": "organization.brand_color", "expected_revision": lock_row.revision + 1}
        ).status_code
        == 409
    )
    assert (
        post(
            {"action": "unlock", "key": "organization.brand_color", "expected_revision": lock_row.revision}
        ).status_code
        == 302
    )
    assert not client.get(settings_url).context["form"].fields["brand_color"].widget.attrs.get("disabled")
    other = login(admin_b)
    assert other.get(url).status_code in (403, 404)


@pytest.mark.needs_example_app
@pytest.mark.closed_app_defaults
def test_rest_api_refuses_a_locked_app(setup):
    from skeleton.system import availability

    platform, org_a, _, _ = setup
    as_platform(platform, lambda: availability.set_app_availability(NOTES, user=platform, mode="all", revision=0))
    lock(platform, org_a, f"apps.{NOTES}.enabled", True)
    client = login(platform)
    url = reverse("api-application-configuration", kwargs={"organization_id": org_a.pk, "app_label": NOTES})
    state = client.get(url)
    assert state.json()["locked_fields"] == ["enabled"]
    response = client.put(
        url,
        json.dumps({"enabled": False, "configuration": state.json()["configuration"]}),
        content_type="application/json",
        headers={"If-Match": f'"{state.json()["revision"]}"', "X-CSRFToken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 409 and response.json()["code"] == "setting_locked"
