"""Serving a verified custom domain as the organization address (#489)."""

import pytest
from django.conf import settings as django_settings
from django.test import Client, override_settings

from skeleton.tenancy import site_domains
from skeleton.tenancy.context import system_context
from skeleton.tenancy.site import SiteAllowedHosts
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario
from tests.site_support import BASE, disable_site, enable_site, host_of, site_client, sites  # noqa: F401

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PORTAL = "portal.cliente.test"
NOTE = "example_notes.note"


@pytest.fixture(autouse=True)
def fresh_cache():
    site_domains.reset_custom_hosts_cache()
    yield
    site_domains.reset_custom_hosts_cache()


@pytest.fixture
def scenario(sites):  # noqa: F811
    from examples.notes.models import Note

    sites.SKELETON_ORGANIZATION_CUSTOM_DOMAINS = True
    scenario = build_service_scenario(Note, slug="portal")
    enable_site(scenario, scenario.a, display_name="Portal Acme")
    with acting_as(scenario.admin_a, scenario.a) as actor:
        record = site_domains.add_site_domain(actor=actor, domain=PORTAL)
    with acting_as(scenario.admin_a, scenario.a) as actor:
        site_domains.verify_site_domain(actor=actor, domain_id=record.pk, resolver=lambda name: [record.challenge])
    scenario.record = record
    site_domains.reset_custom_hosts_cache()
    with override_settings(ALLOWED_HOSTS=SiteAllowedHosts([f".{BASE}", "testserver"])):
        yield scenario


def refused_reference():
    response = Client(HTTP_HOST=f"naoexiste.{BASE}").get("/")
    return response.status_code, response.content


def test_the_custom_domain_serves_the_organization(scenario):
    client = site_client(scenario.member, PORTAL, FIXTURE_PASSWORD)
    page = client.get("/")
    assert page.status_code == 200 and page.wsgi_request.resolver_match.view_name == "web:dashboard"
    assert page.wsgi_request.resolver_match.kwargs["organization_id"] == scenario.a.pk
    html = page.content.decode()
    assert "Portal Acme" in html and f"/o/{scenario.a.pk}" not in html
    assert client.get(f"/tables/{NOTE}/").status_code == 200
    assert client.get(f"/o/{scenario.b.pk}/").status_code == 404
    assert client.get("/platform/").content == b"Not found."


def test_unknown_domain_is_a_bad_request(scenario):
    assert Client(HTTP_HOST="other.cliente.test").get("/").status_code == 400


def test_disabled_address_or_inactive_organization_is_the_neutral_404(scenario):
    disable_site(scenario, scenario.a, revision=1)
    assert (Client(HTTP_HOST=PORTAL).get("/").status_code, Client(HTTP_HOST=PORTAL).get("/").content) == (
        refused_reference()
    )
    enable_site(scenario, scenario.a, revision=2)
    with system_context(reason="test:portal-inactive"):
        scenario.a.is_active = False
        scenario.a.save(update_fields=["is_active"])
    site_domains.reset_custom_hosts_cache()
    assert Client(HTTP_HOST=PORTAL).get("/").status_code == 400  # The claim of an inactive organization is gone.


def test_revoking_the_claim_stops_serving_after_the_cache(scenario):
    assert Client(HTTP_HOST=PORTAL).get("/").status_code == 302  # Fills the per-process cache.
    with acting_as(scenario.admin_a, scenario.a) as actor:
        record = site_domains.site_domains().get()
        site_domains.revoke_site_domain(actor=actor, domain_id=record.pk, expected_revision=record.revision)
    assert Client(HTTP_HOST=PORTAL).get("/").status_code == 302  # Still cached: up to 30 s per process.
    site_domains.reset_custom_hosts_cache()
    assert Client(HTTP_HOST=PORTAL).get("/").status_code == 400


def test_feature_off_accepts_no_custom_domain(scenario, settings):
    settings.SKELETON_ORGANIZATION_CUSTOM_DOMAINS = False
    site_domains.reset_custom_hosts_cache()
    assert Client(HTTP_HOST=PORTAL).get("/").status_code == 400


def test_cache_expired_between_validation_and_classification_is_refused(scenario, monkeypatch):
    from skeleton.tenancy import site as site_module

    real = site_domains.custom_hosts
    calls = []

    def first_then_empty():
        calls.append(1)
        return real() if len(calls) == 1 else {}

    monkeypatch.setattr(site_domains, "custom_hosts", first_then_empty)
    response = Client(HTTP_HOST=PORTAL).get("/")
    assert response.status_code == 404 and response.wsgi_request.skeleton_site_refused is True
    assert site_module.classify_host(PORTAL).kind == "platform"  # What the middleware saw.


def test_login_on_the_custom_domain_keeps_a_host_only_cookie_and_no_sso(scenario):
    client = Client(enforce_csrf_checks=True, HTTP_HOST=PORTAL)
    page = client.get("/accounts/login/").content.decode()
    assert "Use sua conta para acessar Portal Acme." in page
    response = client.post(
        "/accounts/login/",
        {
            "login": scenario.member.email,
            "password": FIXTURE_PASSWORD,
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert response.status_code == 302
    assert response.cookies[django_settings.SESSION_COOKIE_NAME]["domain"] == ""


def test_hsts_on_the_custom_domain_has_no_subdomains(scenario, settings):
    settings.SECURE_HSTS_SECONDS = 31536000
    settings.SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    settings.SECURE_HSTS_PRELOAD = True
    custom = Client(HTTP_HOST=PORTAL).get("/accounts/login/", secure=True)
    assert custom["Strict-Transport-Security"] == "max-age=31536000"
    slug = Client(HTTP_HOST=host_of(scenario.a)).get("/accounts/login/", secure=True)
    assert slug["Strict-Transport-Security"] == "max-age=31536000; includeSubDomains; preload"


def test_links_prefer_the_custom_domain(scenario):
    import uuid

    from skeleton.tenancy.context import request_context
    from skeleton.tenancy.site import organization_url

    with request_context(actor=None, request_id=uuid.uuid4(), source="test:portal-links"):
        assert organization_url(scenario.a.pk, f"/o/{scenario.a.pk}/tables/x/") == f"https://{PORTAL}/tables/x/"


def test_certificate_question_on_the_health_host(scenario, settings):
    settings.SKELETON_HEALTH_HOST = "health.internal.test"
    with override_settings(ALLOWED_HOSTS=SiteAllowedHosts([f".{BASE}", "testserver", "health.internal.test"])):
        health = Client(HTTP_HOST="health.internal.test")
        assert health.get("/health/site-host/", {"domain": PORTAL}).status_code == 200
        assert health.get("/health/site-host/", {"domain": host_of(scenario.a)}).status_code == 200
        for domain in ("unknown.cliente.test", f"naoexiste.{BASE}", "app.example.test", host_of(scenario.b), ""):
            assert health.get("/health/site-host/", {"domain": domain}).status_code == 404, domain
        assert Client().get("/health/site-host/", {"domain": PORTAL}).status_code == 404  # Other host.
        disable_site(scenario, scenario.a, revision=1)
        assert health.get("/health/site-host/", {"domain": PORTAL}).status_code == 404


def test_domain_of_a_never_serves_b(scenario):
    client = site_client(scenario.member, PORTAL, FIXTURE_PASSWORD)
    for path in (f"/o/{scenario.b.pk}/", f"/api/v1/o/{scenario.b.pk}/tables/{NOTE}/records/"):
        assert client.get(path).status_code == 404
