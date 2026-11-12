"""The organization chooses only among available apps; the app manager administers them (#24)."""

import json
from dataclasses import replace

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.system import app_registry
from skeleton.system.app_registry import MenuEntry
from skeleton.system.applications import application_states, configure_application, navigation_for_user
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.applications import activate_app, open_app

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.closed_app_defaults]
PASSWORD = "Org-apps-available-Password!24"
NOTES = "example_notes"


def require_notes():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")


def login(user):
    client = Client()
    assert client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD}).status_code == 302
    return client


def person(owner, organization, email, code):
    from allauth.account.models import EmailAddress

    from skeleton.access.models import MemberRole, Role
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:org-apps-identity"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:org-apps-role"):
        membership = Membership.objects.create(organization=organization, user=user)
        role = Role.objects.get(code=code, is_builtin=True)
        MemberRole.objects.create(organization=organization, membership=membership, role=role)
    return user


def demote(user):
    with system_context(reason="test:org-apps-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def setup():
    require_notes()
    owner, org_a = bootstrap_identity(
        email="org-apps-owner@example.test", organization="org-apps-a", name="A", password=PASSWORD
    )
    owner_b, org_b = bootstrap_identity(
        email="org-apps-owner-b@example.test", organization="org-apps-b", name="B", password=PASSWORD
    )
    demote(owner)
    demote(owner_b)
    manager = person(owner, org_a, "org-apps-manager@example.test", "app_manager")
    member = person(owner, org_a, "org-apps-member@example.test", "member")
    return {"owner": owner, "a": org_a, "owner_b": owner_b, "b": org_b, "manager": manager, "member": member}


def grant(organization):
    from skeleton.system.models import AppAvailability, AppOrganizationGrant

    with system_context(reason="test:org-apps-grant"):
        row = AppAvailability.objects.filter(app_label=NOTES).first()
        if row is None:
            row = AppAvailability(app_label=NOTES, mode="selected", upgrade_state="done")
            row.save()
        existing = AppOrganizationGrant.all_objects.filter(availability=row, granted_organization=organization).first()
        if existing is None:
            AppOrganizationGrant(availability=row, granted_organization=organization).save()
        elif existing.deleted_at is not None:
            existing.restore()


def revoke(organization):
    from skeleton.system.models import AppOrganizationGrant

    with system_context(reason="test:org-apps-revoke"):
        AppOrganizationGrant.objects.get(availability__app_label=NOTES, granted_organization=organization).delete()


def labels(client, organization):
    page = client.get(reverse("application-list", kwargs={"organization_id": organization.pk}))
    assert page.status_code == 200
    api = client.get(reverse("api-application-list", kwargs={"organization_id": organization.pk}))
    assert api.status_code == 200
    html = {app["app_label"]: app for app in page.context["applications"]}
    rest = {app["app_label"]: app for app in api.json()["results"]}
    assert html.keys() == rest.keys()
    return rest


def put(client, organization, *, enabled, revision, configuration=None):
    url = f"/api/v1/o/{organization.pk}/apps/{NOTES}/configuration/"
    body = {"enabled": enabled, "configuration": configuration or {}}
    return client.put(url, json.dumps(body), content_type="application/json", headers={"If-Match": f'"{revision}"'})


def test_unavailable_app_is_hidden_and_refused(setup):
    a = setup["a"]
    client = login(setup["owner"])
    assert NOTES not in labels(client, a)
    base = f"/o/{a.pk}/apps/{NOTES}/"
    assert client.get(base + "admin/").status_code == 404
    assert client.get(base + "configuration/").status_code == 404
    assert client.get(f"/api/v1/o/{a.pk}/apps/{NOTES}/configuration/").status_code == 404
    for enabled in (True, False):
        response = put(client, a, enabled=enabled, revision=0)
        assert response.status_code == 400 and response.json()["code"] == "app_unavailable"
    grant(a)
    assert NOTES in labels(client, a)  # The next request sees the grant.
    assert labels(login(setup["owner_b"]), setup["b"]) == {}


def test_withdrawn_app_is_flagged_blocked_and_can_only_be_closed(setup):
    a = setup["a"]
    grant(a)
    activate_app(NOTES, a.pk)
    client = login(setup["owner"])
    home = reverse("example_notes:home", kwargs={"organization_id": a.pk})
    assert client.get(home).status_code == 200
    revoke(a)
    assert client.get(home).status_code == 404
    listed = labels(client, a)[NOTES]
    assert listed["available"] is False and listed["enabled"] is True
    page = client.get(reverse("application-list", kwargs={"organization_id": a.pk}))
    assert "Indisponibilizada pela plataforma" in page.content.decode()
    assert client.get(f"/o/{a.pk}/apps/{NOTES}/admin/").status_code == 200
    reopened = put(client, a, enabled=True, revision=listed["revision"], configuration=listed["configuration"])
    assert reopened.status_code == 400 and reopened.json()["code"] == "app_unavailable"
    closed = put(client, a, enabled=False, revision=listed["revision"], configuration=listed["configuration"])
    assert closed.status_code == 200 and closed.json()["enabled"] is False
    assert NOTES not in labels(client, a)


def test_app_manager_administers_apps_but_not_people(setup):
    a = setup["a"]
    grant(a)
    client = login(setup["manager"])
    state = labels(client, a)[NOTES]
    enabled = put(client, a, enabled=True, revision=state["revision"], configuration=state["configuration"])
    assert enabled.status_code == 200, enabled.content
    configuration = {**enabled.json()["configuration"], "heading": "Managed by the app manager"}
    configured = put(client, a, enabled=True, revision=enabled.json()["revision"], configuration=configuration)
    assert configured.status_code == 200 and configured.json()["configuration"]["heading"] == configuration["heading"]
    form = client.get(f"/o/{a.pk}/apps/{NOTES}/configuration/")
    assert form.status_code == 200
    disabled = put(client, a, enabled=False, revision=configured.json()["revision"], configuration=configuration)
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    for resource in ("memberships", "roles"):
        kwargs = {"organization_id": a.pk, "resource": resource}
        assert client.get(reverse("web:access-list", kwargs=kwargs)).status_code == 404
        assert client.get(reverse("api:access-list", kwargs=kwargs)).status_code == 404
    assert client.get(reverse("web:organization-settings", kwargs={"organization_id": a.pk})).status_code == 404
    dashboard = client.get(reverse("web:dashboard", kwargs={"organization_id": a.pk}))
    assert dashboard.context["can_manage_applications"] is True
    assert dashboard.context["can_manage_organization"] is False

    member = login(setup["member"])
    assert member.get(reverse("application-list", kwargs={"organization_id": a.pk})).status_code == 404
    assert member.get(reverse("api-application-list", kwargs={"organization_id": a.pk})).status_code == 404
    assert put(member, a, enabled=True, revision=disabled.json()["revision"]).status_code == 404


def test_app_manager_of_a_never_reaches_b_and_revocation_is_immediate(setup):
    a, b = setup["a"], setup["b"]
    grant(a)
    grant(b)
    client = login(setup["manager"])
    assert client.get(reverse("application-list", kwargs={"organization_id": b.pk})).status_code == 404
    assert put(client, b, enabled=True, revision=0).status_code == 404
    with tenant_context(organization_id=b.pk, actor=setup["owner_b"], source="test:org-apps-b-state"):
        assert application_states()[NOTES]["enabled"] is False
    assert NOTES in labels(client, a)
    revoke(a)
    assert NOTES not in labels(client, a)


def test_states_are_memoized_per_request_and_refreshed_after_a_write(setup):
    a, owner = setup["a"], setup["owner"]
    grant(a)
    with request_context(actor=owner, request_id=None, source="test:org-apps-memo"):
        with tenant_context(organization_id=a.pk, actor=owner, source="test:org-apps-memo-tenant"):
            first = application_states()
            with CaptureQueriesContext(connection) as queries:
                assert application_states() is first
            assert len(queries) == 0
            configure_application(
                NOTES, user=owner, enabled=True, configuration=first[NOTES]["configuration"], revision=0
            )
            assert application_states()[NOTES]["enabled"] is True
    # The memo dies with its request: the next one reads the database again.
    with tenant_context(organization_id=a.pk, actor=owner, source="test:org-apps-memo-after"):
        after = application_states()
    with tenant_context(organization_id=a.pk, actor=owner, source="test:org-apps-memo-next"):
        assert application_states() is not after


def test_navigation_queries_do_not_grow_with_apps(setup, monkeypatch):
    from skeleton.access.models import Role, TablePermission
    from skeleton.system.models import TableDefinition

    a, owner = setup["a"], setup["owner"]
    notes = app_registry.get_app(NOTES)
    entry = MenuEntry("Notes", "example_notes:home", "example_notes.note")
    open_app(NOTES)
    activate_app(NOTES, a.pk)
    with tenant_context(organization_id=a.pk, actor=owner, source="test:org-apps-nav-permission"):
        TablePermission.objects.get_or_create(
            organization=a,
            role=Role.objects.get(code="organization_admin", is_builtin=True),
            table=TableDefinition.objects.get(model_label="example_notes.note"),
            action="read",
        )
    registered, counts = [], {}
    for count in (1, 5, 20):
        while len(registered) < count:
            label = f"nav_probe_{len(registered):02d}"
            fake = replace(notes, app_label=label, app_id=label, label=label, package_name=None, navigation=(entry,))
            monkeypatch.setitem(app_registry._manifests, label, fake)
            open_app(label)
            activate_app(label, a.pk)
            registered.append(label)
        with tenant_context(organization_id=a.pk, actor=owner, source="test:org-apps-nav"):
            with CaptureQueriesContext(connection) as queries:
                menu = navigation_for_user(owner)
        assert len([item for item in menu if item["app_label"] in registered]) == count
        counts[count] = len(queries)
    assert counts[1] == counts[5] == counts[20], counts
