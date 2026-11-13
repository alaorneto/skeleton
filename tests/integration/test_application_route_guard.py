"""The core guard of app routes (#322): login, organization, activation and the administrative MFA gate.

Runs on PostgreSQL with the runtime role, on the example app's HTML and API routes.
"""

import pytest
from django.test import Client
from django.urls import reverse

from examples.notes.models import Note
from skeleton.testing.service_api import build_service_scenario
from tests import support

pytestmark = pytest.mark.needs_example_app


@pytest.fixture
def scenario(skeleton_db):
    return build_service_scenario(Note, slug="route-guard")


def page(organization):
    return reverse("example_notes:home", kwargs={"organization_id": organization.pk})


def api(organization):
    return reverse("example_notes_api:summary", kwargs={"organization_id": organization.pk})


def login(scenario, user):
    return support.login(user, password=scenario.password)


def test_anonymous_on_an_app_page_is_sent_to_login(scenario):
    url = page(scenario.a)
    response = Client().get(url)
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("account_login")) and "next=" in response["Location"]
    assert url.replace("/", "%2F") in response["Location"] or url in response["Location"]


def test_anonymous_on_an_app_api_route_is_refused(scenario):
    """Characterization: the API answers 403 JSON to an anonymous request, as before."""
    response = Client().get(api(scenario.a))
    assert response.status_code == 403 and response.json()["code"] == "permission_denied"


def test_capability_holder_without_mfa_is_gated_on_app_pages(scenario, settings):
    settings.SKELETON_REQUIRE_ADMIN_MFA = True
    client = login(scenario, scenario.admin_a)
    response = client.get(page(scenario.a))
    assert response.status_code == 302 and response["Location"] == reverse("mfa_index")


def test_capability_holder_without_mfa_is_gated_on_app_api_routes(scenario, settings):
    settings.SKELETON_REQUIRE_ADMIN_MFA = True
    client = login(scenario, scenario.admin_a)
    response = client.get(api(scenario.a))
    assert response.status_code == 403 and response.json()["code"] == "mfa_required"


def test_plain_member_is_not_gated_by_mfa(scenario, settings):
    settings.SKELETON_REQUIRE_ADMIN_MFA = True
    client = login(scenario, scenario.member)
    assert client.get(page(scenario.a)).status_code == 200
    assert client.get(api(scenario.a)).status_code == 200


def test_disabled_app_keeps_its_answer_before_the_mfa_gate(scenario, settings):
    """A disabled app keeps its answer (403 HTML, 404 API), never the MFA redirect, for everyone."""
    settings.SKELETON_REQUIRE_ADMIN_MFA = True
    with scenario.application_disabled():
        for user in (scenario.admin_a, scenario.member):
            client = login(scenario, user)
            assert client.get(page(scenario.a)).status_code == 403
            assert client.get(api(scenario.a)).status_code == 404


def _guarded(view, *, api_route=False):
    """The core guard around a plain view that applies no gate of its own."""
    from django.urls import path

    from skeleton.system.administration import guard_application_api_urls, guard_application_urls
    from skeleton.system.app_registry import get_app

    guard = guard_application_api_urls if api_route else guard_application_urls
    [pattern] = guard([path("plain/", view, name="plain")], get_app("example_notes"))
    return pattern.callback


def _request(user):
    from types import SimpleNamespace

    from django.test import RequestFactory

    request = RequestFactory().get("/plain/")
    request.user = user
    request.resolver_match = SimpleNamespace(view_name="example_notes:plain")
    return request


def test_the_guard_itself_applies_the_mfa_gate_to_views_without_one(scenario, settings):
    """An app view written without tenant_page/TenantAPI is still behind the administrative MFA gate."""
    from django.http import HttpResponse

    from skeleton.testing.service_api import acting_as

    settings.SKELETON_REQUIRE_ADMIN_MFA = True
    reached = []

    def plain(request):
        reached.append(request.user.pk)
        return HttpResponse("ok")

    for api_route in (False, True):
        with acting_as(scenario.admin_a, scenario.a):
            response = _guarded(plain, api_route=api_route)(_request(scenario.admin_a))
        if api_route:
            assert response.status_code == 403 and b"mfa_required" in response.content
        else:
            assert response.status_code == 302 and response["Location"] == reverse("mfa_index")
        with acting_as(scenario.member, scenario.a):
            assert _guarded(plain, api_route=api_route)(_request(scenario.member)).status_code == 200
    assert reached == [scenario.member.pk, scenario.member.pk]


def test_non_member_still_gets_404(scenario):
    client = login(scenario, scenario.outsider)
    assert client.get(page(scenario.a)).status_code == 404
    assert client.get(api(scenario.a)).status_code in (403, 404)
