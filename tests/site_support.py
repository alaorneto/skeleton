"""Shared fixtures of the organization site tests (#477): base domain settings and enabled sites."""

import pytest
from django.test import Client
from django.urls import reverse

BASE = "example.test"
PLATFORM_HOST = "app.example.test"
BLANK = {"display_name": "", "default_language": "", "default_timezone": "", "brand_color": ""}


def site_settings(settings):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = BASE
    settings.SKELETON_PUBLIC_URL = f"https://{PLATFORM_HOST}"
    settings.ALLOWED_HOSTS = [f".{BASE}", "testserver"]
    return settings


@pytest.fixture
def sites(settings):
    return site_settings(settings)


def host_of(organization):
    return f"{organization.slug}.{BASE}"


def enable_site(scenario, organization, *, revision=0, **values):
    """Turn the address of ``organization`` on through the administrator, like the settings page does."""
    from skeleton.tenancy.organization_settings import update_settings
    from skeleton.testing.service_api import acting_as

    admin = scenario.admin_a if organization.pk == scenario.a.pk else scenario.admin_b
    with acting_as(admin, organization) as actor:
        return update_settings(actor=actor, data={**BLANK, **values, "site_enabled": True}, expected_revision=revision)


def disable_site(scenario, organization, *, revision):
    from skeleton.tenancy.organization_settings import update_settings
    from skeleton.testing.service_api import acting_as

    admin = scenario.admin_a if organization.pk == scenario.a.pk else scenario.admin_b
    with acting_as(admin, organization) as actor:
        return update_settings(actor=actor, data={**BLANK, "site_enabled": False}, expected_revision=revision)


def site_client(user, host, password):
    """A client on ``host`` logged in through the real form (CSRF enforced)."""
    client = Client(enforce_csrf_checks=True, HTTP_HOST=host)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": password, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302, (host, response.status_code)
    return client
