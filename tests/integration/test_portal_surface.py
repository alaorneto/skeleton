"""The portal surface (#533): routes, the external context from the URL, the app gate and the billing standing.

Runs on PostgreSQL with the runtime role. The fixture app's portal routes are mounted by
``portal_fixture_urls`` exactly as ``app_routing`` mounts a manifest's ``portal_urls``.
"""

import pytest
from django.test import Client
from django.urls import Resolver404, resolve, reverse

from skeleton.testing.external import external_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app, pytest.mark.no_api_contract]
APP = "example_notes"


@pytest.fixture
def scenario(settings):
    from skeleton.testing.applications import activate_app, open_app

    settings.ROOT_URLCONF = "portal_fixture_urls"
    found = external_scenario("ext-portal")
    open_app(APP)
    activate_app(APP, found.a.pk)
    return found


def login(user, password):
    from allauth.account.models import EmailAddress

    from skeleton.tenancy.context import system_context
    from tests import support

    with system_context(reason="test:verified-email"):
        EmailAddress.objects.get_or_create(user=user, email=user.email, defaults={"verified": True, "primary": True})
    return support.login(user, password=password)


def app_url(organization, name="hello"):
    return reverse(f"{APP}_portal:{name}", kwargs={"organization_id": organization.pk})


def test_the_portal_lists_only_the_persons_open_portals(scenario):
    from skeleton.testing.external import enable_portal, grant_external_access

    grant_external_access(scenario.b, scenario.external)
    enable_portal(scenario.b, enabled=False)
    assert Client().get(reverse("web:portal")).status_code == 302  # Login first.
    html = login(scenario.external, scenario.password).get(reverse("web:portal")).content.decode()
    assert scenario.a.name in html and scenario.b.name not in html
    member = login(scenario.member, scenario.password).get(reverse("web:portal")).content.decode()
    assert scenario.a.name not in member


def test_only_an_external_person_reaches_the_portal_of_an_active_app(scenario):
    external = login(scenario.external, scenario.password)
    page = external.get(app_url(scenario.a))
    assert page.status_code == 200 and str(scenario.external.pk) in page.content.decode()
    assert "no-cache" in page["Cache-Control"] or "no-store" in page["Cache-Control"]
    home = external.get(reverse("web:portal-organization", kwargs={"organization_id": scenario.a.pk}))
    assert home.status_code == 200 and "<script" not in home.content.decode()
    for user in (scenario.member, scenario.outsider):
        assert login(user, scenario.password).get(app_url(scenario.a)).status_code == 404
    assert external.get(app_url(scenario.b)).status_code == 404  # No access in B.
    assert external.get(f"/portal/{scenario.a.pk}/apps/other_app/hello/").status_code == 404  # No portal_urls.
    from skeleton.system.applications import application_state, configure_application
    from skeleton.testing.service_api import acting_as

    with acting_as(scenario.admin_a, scenario.a):
        state = application_state(APP)
        configure_application(
            APP, user=scenario.admin_a, enabled=False, configuration=state["configuration"], revision=state["revision"]
        )
    assert external.get(app_url(scenario.a)).status_code == 404  # The app is off.


def test_portal_and_member_routes_never_cross(scenario):
    portal_path = app_url(scenario.a)
    member_path = portal_path.replace("/portal/", "/o/", 1)
    with pytest.raises(Resolver404):
        resolve(member_path)
    with pytest.raises(Resolver404):
        resolve(f"/portal/{scenario.a.pk}/admin/external/")
    dual = login(scenario.dual, scenario.password)
    assert dual.get(f"/o/{scenario.a.pk}/admin/external/").status_code == 200  # As a member.
    assert dual.get(portal_path).status_code == 200  # As external, in the portal.


def test_the_billing_standing_closes_or_freezes_the_portal(scenario, monkeypatch):
    from skeleton.common import entitlements
    from skeleton.common.entitlements import AccessLevel

    external = login(scenario.external, scenario.password)
    csrf = external.cookies["csrftoken"].value
    monkeypatch.setattr(entitlements, "organization_access", lambda organization_id: AccessLevel.RESTRICTED)
    assert external.get(app_url(scenario.a)).status_code == 200
    assert external.post(app_url(scenario.a), {"csrfmiddlewaretoken": csrf}).status_code == 403
    monkeypatch.setattr(entitlements, "organization_access", lambda organization_id: AccessLevel.SUSPENDED)
    closed = external.get(app_url(scenario.a))
    assert closed.status_code == 503 and "indisponível" in closed.content.decode()


def test_csrf_and_the_portal_api(scenario):
    api = login(scenario.external, scenario.password)  # Enforces CSRF, like a browser.
    assert api.post(app_url(scenario.a)).status_code == 403  # No CSRF token.
    path = reverse(f"{APP}_portal_api:me", kwargs={"organization_id": scenario.a.pk})
    response = api.get(path)
    assert response.status_code == 200 and response.json()["user_id"] == str(scenario.external.pk)
    assert response["Cache-Control"] == "no-store"
    member = login(scenario.member, scenario.password)
    assert member.get(path).status_code == 404
    assert Client().get(path).status_code == 404


def test_the_portal_api_stays_out_of_the_members_schema(scenario):
    from skeleton.api.openapi import api_routes

    members = {route.path for route in api_routes("portal_fixture_urls")}
    portal = {route.path for route in api_routes("portal_fixture_urls", portal=True)}
    assert not any(path.startswith("/api/v1/portal/") for path in members)
    assert portal == {f"/api/v1/portal/{{organization_id}}/apps/{APP}/me/"}
