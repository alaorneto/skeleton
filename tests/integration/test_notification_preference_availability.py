"""The person's preferences list only the notices they can receive, grouped by context (#790)."""

import json
from uuid import uuid4

import pytest
from django.urls import reverse

from skeleton.notifications.preferences import available_kinds
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import FIXTURE_PASSWORD
from tests import support
from tests.integration.appearance_support import PASSWORD, login, member, platform_admin

pytestmark = pytest.mark.usefixtures("skeleton_db")


@pytest.fixture
def world():
    from skeleton.testing.people import add_member, make_organization, make_person

    with isolated_notification_types():
        register_notification_type(
            NotificationType(
                "organization.test",
                "Da organização",
                owner_app="sk_tenancy",
                context="organization",
                description="Uma frase",
            )
        )
        register_notification_type(NotificationType("agenda.test", "Da agenda", owner_app="agenda_app"))
        register_notification_type(NotificationType("tasks.test", "Das tarefas", owner_app="tasks_app"))
        register_notification_type(
            NotificationType(
                "portal.test", "Externo", owner_app="agenda_app", audience="external", default_channels=("email",)
            )
        )
        common = {"owner_app": "sk_jobs", "context": "platform", "audience": "platform"}
        register_notification_type(NotificationType("platform.test_admins", "Só administradores", **common))
        register_notification_type(
            NotificationType("platform.test_ops", "Operadores", platform_capability="updates.operate", **common)
        )
        person = make_person(email="prefs-person@example.test")
        owner = make_person(email="prefs-owner@example.test")
        organization = make_organization(owner=owner, slug="prefs-org", name="Prefs")
        add_member(organization=organization, user=person)
        yield person, organization


def snapshot(organization, apps):
    from skeleton.insights.collect import refresh_organization
    from skeleton.insights.models import OrganizationSnapshot

    with system_context(reason="test:prefs-snapshot", organization_id=organization.pk):
        refresh_organization()
    with system_context(reason="test:prefs-snapshot-apps"):
        OrganizationSnapshot.all_objects.filter(subject_organization=organization).update(apps_effective=apps)


def codes(user):
    with request_context(actor=user, request_id=uuid4(), source="test:prefs"):
        return {kind.code for kind in available_kinds(user)}


def test_app_notices_follow_the_apps_active_in_the_person_s_organizations(world):
    person, organization = world
    snapshot(organization, ["agenda_app"])
    assert codes(person) == {"organization.test", "agenda.test"}  # Not the tasks app, never the external one.


def test_without_a_summary_every_installed_app_counts(world):
    person, _organization = world
    assert {"agenda.test", "tasks.test"} <= codes(person)


def test_platform_notices_only_for_who_receives_them(world):
    from skeleton.accounts.platform import grant_platform_role

    person, organization = world
    snapshot(organization, [])
    assert not {code for code in codes(person) if code.startswith("platform.")}
    admin = platform_admin("prefs-admin@example.test")
    assert {"platform.test_admins", "platform.test_ops"} <= codes(admin)
    operator = member("prefs-operator@example.test")
    with request_context(actor=admin, request_id=uuid4(), source="test:prefs-roles"):
        grant_platform_role(actor=admin, user=operator, role="operator")
    assert {code for code in codes(operator) if code.startswith("platform.")} == {"platform.test_ops"}


def test_turned_off_in_the_instance_disappears(world):
    from skeleton.notifications import instance_settings
    from tests.integration.appearance_support import acting

    person, organization = world
    snapshot(organization, [])
    admin = platform_admin("prefs-off@example.test")
    with acting(admin):
        instance_settings.set_instance_setting(
            actor=admin,
            type_code="organization.test",
            enabled=False,
            default_channels=["in_app"],
            expected_revision=None,
        )
    assert "organization.test" not in codes(person)


def test_page_and_api(world, subtests):
    person, organization = world
    snapshot(organization, ["agenda_app"])
    client = support.login(person, password=FIXTURE_PASSWORD)
    with subtests.test("groups, descriptions and no codes"):
        page = client.get(reverse("notifications:preferences")).content.decode()
        assert "Da organização" in page and "Uma frase" in page and "Dos aplicativos" in page
        assert "<code>organization.test</code>" not in page and "Das tarefas" not in page
    with subtests.test("the API lists the same and refuses an unavailable type"):
        listing = client.get(reverse("notifications:api-preferences")).json()["results"]
        assert {item["type_code"] for item in listing} == {"*", "organization.test", "agenda.test"}
        assert {item["context"] for item in listing if item["type_code"] == "agenda.test"} == {"app"}
        url = reverse("notifications:api-preference", kwargs={"type_code": "tasks.test", "channel": "email"})
        token = client.cookies["csrftoken"].value
        response = client.put(url, json.dumps({"mode": "off"}), content_type="application/json", HTTP_X_CSRFTOKEN=token)
        assert response.status_code == 404
    with subtests.test("push is not available for the platform's notices"):
        admin = platform_admin("prefs-page-admin@example.test")
        admin_client = login(admin.email, PASSWORD)
        page = admin_client.get(reverse("notifications:preferences")).content.decode()
        assert "Não disponível" in page and "mode:platform.test_admins:push" not in page
