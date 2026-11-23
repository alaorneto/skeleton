"""Configuration fields of an app only the platform writes (#702).

PostgreSQL with the runtime role. The organization sees the field read-only and a forged
change is refused with ``setting_locked``; the platform writes it per organization (the row
is born disabled), with CAS; the app reads it; A's value never reaches B.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.service_api.applications import ApplicationAdministrationService
from skeleton.service_api.errors import Conflict
from skeleton.system import app_registry, availability
from skeleton.system.app_registry import ConfigField
from skeleton.system.applications import configure_application_by_platform
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.applications import activate_app
from skeleton.testing.service_api import acting_as

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.closed_app_defaults, pytest.mark.needs_example_app]
PASSWORD = "Managed-fields-Password!702"
NOTES = "example_notes"


def demote(user):
    with system_context(reason="test:managed-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def setup(monkeypatch):
    fields = (ConfigField("region", str, "sa-east-1", managed_by="platform"), ConfigField("greeting", str, "Olá"))
    monkeypatch.setitem(
        app_registry._manifests, NOTES, replace(app_registry.get_app(NOTES), configuration_fields=fields)
    )
    platform, org_a = bootstrap_identity(
        email="managed-platform@example.test", organization="managed-a", name="A", password=PASSWORD
    )
    admin_b, org_b = bootstrap_identity(
        email="managed-b@example.test", organization="managed-b", name="B", password=PASSWORD
    )
    demote(admin_b)
    admin_b.refresh_from_db()
    with request_context(actor=platform, request_id=uuid4(), source="test:managed-offer"):
        availability.set_app_availability(NOTES, user=platform, mode="all", revision=0)
    return platform, org_a, admin_b, org_b


def by_platform(platform, organization, values, revision):
    with request_context(actor=platform, request_id=uuid4(), source="test:managed-platform"):
        return configure_application_by_platform(
            NOTES, user=platform, organization_id=organization.pk, values=values, revision=revision
        )


def stored(organization):
    from skeleton.system.models import AppInstallation

    with system_context(reason="test:managed-read", organization_id=organization.pk):
        record = AppInstallation.objects.filter(app_label=NOTES).first()
        return (record.enabled, record.configuration, record.revision) if record else None


def test_platform_writes_and_organization_cannot(setup):
    platform, org_a, admin_b, org_b = setup
    record = by_platform(platform, org_a, {"region": "us-east-1"}, 0)  # App not enabled: born disabled.
    assert stored(org_a) == (False, {"region": "us-east-1", "greeting": "Olá"}, record.revision)
    assert stored(org_b) is None  # B is untouched.
    with pytest.raises(ConcurrentUpdate):
        by_platform(platform, org_a, {"region": "eu-west-1"}, 0)
    with pytest.raises(ValidationError):  # An organization field is not the platform's.
        by_platform(platform, org_a, {"greeting": "Oi"}, record.revision)
    with request_context(actor=admin_b, request_id=uuid4(), source="test:managed-denied"):
        with pytest.raises(PermissionDenied):
            configure_application_by_platform(NOTES, user=admin_b, organization_id=org_b.pk, values={}, revision=0)
    activate_app(NOTES, org_a.pk)
    by_platform(platform, org_a, {"region": "eu-west-1"}, stored(org_a)[2])  # And with the app active.
    service = ApplicationAdministrationService()
    with acting_as(platform, org_a) as actor:
        state = service.application(actor=actor, app_label=NOTES)
        assert "region" in state.locked_fields
        with pytest.raises(Conflict) as forged:
            service.configure(
                actor=actor,
                app_label=NOTES,
                enabled=True,
                configuration=(("region", "ap-south-1"), ("greeting", "Oi")),
                expected_revision=state.revision,
            )
        assert forged.value.code == "setting_locked"
        changed = service.configure(  # The other field still changes; the platform's is kept.
            actor=actor,
            app_label=NOTES,
            enabled=True,
            configuration=(("greeting", "Oi"),),
            expected_revision=state.revision,
        )
        assert {item.name: item.value for item in changed.configuration} == {"region": "eu-west-1", "greeting": "Oi"}


def test_the_app_reads_the_platform_value(setup, monkeypatch):
    from skeleton.service_api import applications

    platform, org_a, _, _ = setup
    activate_app(NOTES, org_a.pk)
    by_platform(platform, org_a, {"region": "us-east-1"}, stored(org_a)[2])
    monkeypatch.setattr(applications, "calling_app_label", lambda: NOTES)
    with acting_as(platform, org_a) as actor:
        configuration = applications.ApplicationService().my_configuration(actor=actor, app_label=NOTES)
    assert {item.name: item.value for item in configuration.values}["region"] == "us-east-1"


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def test_pages(setup):
    platform, org_a, admin_b, _ = setup
    activate_app(NOTES, org_a.pk)
    client = login(platform)
    url = reverse("platform:organization-app-configuration", kwargs={"sysid": org_a.pk, "app_label": NOTES})

    def post(data, who=client, target=url):
        return who.post(target, {**data, "csrfmiddlewaretoken": who.cookies["csrftoken"].value})

    page = client.get(url)
    assert page.status_code == 200 and set(page.context["form"].fields) == {"region", "expected_revision"}
    revision = page.context["form"]["expected_revision"].value()
    assert post({"region": "us-east-1", "expected_revision": revision + 5}).status_code == 409
    assert post({"region": "us-east-1", "expected_revision": revision}).status_code == 302
    assert stored(org_a)[1]["region"] == "us-east-1"
    # The organization's configuration page shows the field read-only.
    configuration = reverse("application-configuration", kwargs={"organization_id": org_a.pk, "app_label": NOTES})
    shown = client.get(configuration)
    assert shown.context["form"].fields["region"].widget.attrs.get("disabled")
    assert login(admin_b).get(url).status_code in (403, 404)
