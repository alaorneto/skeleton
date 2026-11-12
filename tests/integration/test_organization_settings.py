"""Organization settings (#167): administrator only, CAS from virtual revision 0, audited, A/B isolated."""

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.service_api.errors import Forbidden, InvalidInput
from skeleton.service_api.organizations import OrganizationService
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import (
    FIXTURE_PASSWORD,
    acting_as,
    assert_cas_conflict,
    assert_rejects_other_actor,
    assert_tenant_isolated,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
SERVICE = OrganizationService()
VALUES = {
    "display_name": "Acme Brasil",
    "default_language": "pt-br",
    "default_timezone": "America/Sao_Paulo",
    "brand_color": "#1D4ED8",
}


@pytest.fixture
def scenario():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="org-settings")


def update(actor, revision, **changes):
    return SERVICE.update_settings(actor=actor, **{**VALUES, **changes}, expected_revision=revision)


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": FIXTURE_PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def test_settings_start_blank_and_the_admin_writes_them(scenario):
    with acting_as(scenario.member, scenario.a) as member:
        blank = SERVICE.settings(actor=member)
    assert (blank.revision, blank.display_name, blank.has_logo) == (0, "", False)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        saved = update(admin, 0)
        assert saved.revision == 1
        assert saved.brand_color == "#1d4ed8"  # Normalized to lowercase.
        assert update(admin, 1).revision == 1  # Same values: nothing written.
        assert update(admin, 1, display_name="Acme").revision == 2
    with acting_as(scenario.member, scenario.a) as member:
        assert SERVICE.settings(actor=member).display_name == "Acme"


def test_only_the_administrator_changes_settings(scenario):
    with acting_as(scenario.member, scenario.a) as member:
        with pytest.raises(Forbidden):
            update(member, 0)
        assert SERVICE.settings(actor=member).revision == 0
    assert_rejects_other_actor(scenario.caller(lambda actor: update(scenario.admin_a, 0)))


def test_first_writes_race_on_virtual_revision_zero_and_later_on_the_row(scenario):
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert_cas_conflict(lambda: update(admin, 0), lambda: update(admin, 0, display_name="Outro"))
        assert_cas_conflict(lambda: update(admin, 1, display_name="B"), lambda: update(admin, 1, display_name="C"))
        assert SERVICE.settings(actor=admin).display_name == "B"


@pytest.mark.parametrize(
    "field, value",
    [
        ("default_language", "xx"),
        ("default_timezone", "Mars/Olympus"),
        ("brand_color", "blue"),
        ("brand_color", "#12345"),
        ("display_name", "x" * 201),
    ],
)
def test_invalid_values_are_refused(scenario, field, value):
    with acting_as(scenario.admin_a, scenario.a) as admin:
        with pytest.raises(InvalidInput) as error:
            update(admin, 0, **{field: value})
        assert field in error.value.errors
        assert SERVICE.settings(actor=admin).revision == 0


def test_organizations_do_not_share_settings(scenario):
    with acting_as(scenario.admin_a, scenario.a) as admin:
        update(admin, 0, display_name="Marca de A")
    with acting_as(scenario.admin_b, scenario.b) as admin_b:
        other = assert_tenant_isolated(lambda: SERVICE.settings(actor=admin_b), foreign=["Marca de A"])
        assert other.revision == 0
        update(admin_b, 0, display_name="Marca de B")
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert SERVICE.settings(actor=admin).display_name == "Marca de A"


def test_changes_are_audited(scenario):
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.models import OrganizationSettings

    with acting_as(scenario.admin_a, scenario.a) as admin:
        update(admin, 0)
        update(admin, 1, display_name="Acme 2")
    with system_context(reason="test:org-settings-audit", organization_id=scenario.a.pk):
        record = OrganizationSettings.objects.get()
        events = list(
            AuditEvent.objects.filter(record_sysid=record.pk)
            .order_by("record_revision")
            .values_list("operation", "changes")
        )
    assert [operation for operation, _ in events] == ["create", "update"]
    assert events[1][1]["display_name"]["new"] == "Acme 2"
    assert events[0][1]["logo_key"]["redacted"] is True


def test_settings_page_for_the_administrator(scenario):
    url = reverse("web:organization-settings", kwargs={"organization_id": scenario.a.pk})
    client = login(scenario.admin_a)
    page = client.get(url)
    assert page.status_code == 200
    assert page["Cache-Control"].startswith("max-age=0")
    token = client.cookies["csrftoken"].value
    data = {**VALUES, "revision": "0", "csrfmiddlewaretoken": token}
    assert client.post(url, data).status_code == 302
    assert client.post(url, data).status_code == 409  # Stale revision.
    assert client.post(url, {**data, "revision": "1", "brand_color": "azul"}).status_code == 400
    assert client.post(url, {**data, "revision": "1", "logo_key": "x"}).status_code == 400
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert SERVICE.settings(actor=admin).display_name == "Acme Brasil"


def test_settings_page_is_closed_to_members_and_other_organizations(scenario):
    url = reverse("web:organization-settings", kwargs={"organization_id": scenario.a.pk})
    member = login(scenario.member)
    assert member.get(url).status_code == 403
    assert (
        member.post(
            url, {**VALUES, "revision": "0", "csrfmiddlewaretoken": member.cookies["csrftoken"].value}
        ).status_code
        == 403
    )
    assert login(scenario.admin_b).get(url).status_code == 404
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert SERVICE.settings(actor=admin).revision == 0
