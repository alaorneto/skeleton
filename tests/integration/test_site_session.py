"""The session cookie is shared by every host under the base domain, host-only elsewhere (#481)."""

import pytest
from django.conf import settings as django_settings
from django.test import Client
from django.urls import reverse

from skeleton.tenancy.organization_settings import update_settings
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PLATFORM, ORGANIZATION, BARE, OUTSIDE = "app.example.test", "sess-a.example.test", "example.test", "portal.cliente.test"


@pytest.fixture
def sites(settings):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    settings.ALLOWED_HOSTS = [".example.test", "testserver", OUTSIDE]
    return settings


@pytest.fixture
def scenario(sites):
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="sess")
    with acting_as(scenario.admin_a, scenario.a) as admin:
        update_settings(
            actor=admin,
            data={"display_name": "", "default_language": "", "default_timezone": "", "brand_color": ""}
            | {"site_enabled": True},
            expected_revision=0,
        )
    return scenario


def login(user, host):
    client = Client(enforce_csrf_checks=True, HTTP_HOST=host)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": FIXTURE_PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302, host
    return client, response


def session_cookie(response):
    return response.cookies[django_settings.SESSION_COOKIE_NAME]


@pytest.mark.parametrize("host", [PLATFORM, ORGANIZATION, BARE])
def test_login_inside_the_base_domain_shares_the_cookie(scenario, host):
    client, response = login(scenario.member, host)
    cookie = session_cookie(response)
    assert cookie["domain"] == ".example.test"
    assert cookie["httponly"] is True and cookie["samesite"] == "Lax"
    assert bool(cookie["secure"]) is bool(django_settings.SESSION_COOKIE_SECURE)
    assert client.cookies["csrftoken"]["domain"] == ""  # The CSRF cookie is never shared.
    logout = client.post(reverse("account_logout"), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    expired = session_cookie(logout)
    assert expired["domain"] == ".example.test" and expired.value == ""
    assert "1970" in expired["expires"]


def test_login_outside_the_base_domain_keeps_a_host_only_cookie(scenario):
    _client, response = login(scenario.member, OUTSIDE)
    assert session_cookie(response)["domain"] == ""


def test_without_base_domain_nothing_changes(scenario, settings):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = ""
    _client, response = login(scenario.member, "testserver")
    assert session_cookie(response)["domain"] == ""


def test_a_response_without_session_cookie_gets_no_header(scenario):
    response = Client(HTTP_HOST=PLATFORM).get("/health/live/")
    assert django_settings.SESSION_COOKIE_NAME not in response.cookies


def test_disallowed_host_does_not_break_the_middleware(scenario):
    response = Client(HTTP_HOST="evil.invalid").get(reverse("account_login"))
    assert response.status_code == 400


@pytest.mark.parametrize(("origin", "status"), [("http://outra.example.test", 403), (f"http://{ORGANIZATION}", 302)])
def test_csrf_origin_is_checked_per_host(scenario, origin, status):
    client = Client(enforce_csrf_checks=True, HTTP_HOST=ORGANIZATION)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {
            "login": scenario.member.email,
            "password": FIXTURE_PASSWORD,
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
        HTTP_ORIGIN=origin,
    )
    assert response.status_code == status
