"""The organization's brand on its own address (#484): anonymous pages, the public logo, title and footer."""

import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from skeleton.tenancy.branding import replace_logo
from skeleton.tenancy.context import system_context
from skeleton.tenancy.site import find_site
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario
from tests.site_support import disable_site, enable_site, host_of, site_client, sites  # noqa: F401

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
NOTE = "example_notes.note"
PNG_A = b"\x89PNG\r\n\x1a\n" + b"A" * 80
PNG_B = b"\x89PNG\r\n\x1a\n" + b"B" * 80
TITLE = re.compile(r"<title>(.*?)</title>", re.S)


@pytest.fixture
def scenario(sites):  # noqa: F811
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="brand")
    enable_site(scenario, scenario.a, display_name="Acme Pública", brand_color="#0f766e")
    enable_site(scenario, scenario.b, display_name="Beta Pública")
    return scenario


def upload_logo(admin, organization, content, revision):
    with acting_as(admin, organization) as actor:
        return replace_logo(actor=actor, upload=SimpleUploadedFile("logo.png", content), expected_revision=revision)


def title(response):
    return TITLE.search(response.content.decode()).group(1).strip()


def test_anonymous_login_page_shows_the_brand(scenario):
    upload_logo(scenario.admin_a, scenario.a, PNG_A, 1)
    page = Client(HTTP_HOST=host_of(scenario.a)).get("/accounts/login/")
    html = page.content.decode()
    assert page.status_code == 200
    assert title(page) == "Entrar · Acme Pública"
    assert "Acme Pública" in html and 'src="/site/logo/?v=' in html
    assert "--color-brand:#0f766e" in html  # The palette, inside the nonce'd <style>.
    assert '<meta name="theme-color" content="#0f766e">' in html
    assert "Skeleton" not in html


def test_public_logo_route(scenario):
    upload_logo(scenario.admin_a, scenario.a, PNG_A, 1)
    response = Client(HTTP_HOST=host_of(scenario.a)).get("/site/logo/")
    assert response.status_code == 200 and response.content == PNG_A
    assert response["Content-Type"] == "image/png"
    assert response["Cache-Control"] == "public, max-age=300"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in response["Content-Security-Policy"]
    assert Client(HTTP_HOST=host_of(scenario.a)).post("/site/logo/").status_code == 405
    assert Client(HTTP_HOST=host_of(scenario.b)).get("/site/logo/").status_code == 404  # B has no logo.
    assert Client().get("/site/logo/").status_code == 404  # Main domain.


def test_each_host_serves_its_own_logo(scenario):
    upload_logo(scenario.admin_a, scenario.a, PNG_A, 1)
    upload_logo(scenario.admin_b, scenario.b, PNG_B, 1)
    assert Client(HTTP_HOST=host_of(scenario.a)).get("/site/logo/").content == PNG_A
    assert Client(HTTP_HOST=host_of(scenario.b)).get("/site/logo/").content == PNG_B
    disable_site(scenario, scenario.a, revision=2)
    assert Client(HTTP_HOST=host_of(scenario.a)).get("/site/logo/").content == b"Not found."
    assert Client(HTTP_HOST=host_of(scenario.b)).get("/site/logo/").content == PNG_B


def test_a_tampered_key_in_the_index_is_refused(scenario):
    from skeleton.tenancy.models import OrganizationSite

    upload_logo(scenario.admin_a, scenario.a, PNG_A, 1)
    record = upload_logo(scenario.admin_b, scenario.b, PNG_B, 1)
    with system_context(reason="test:brand-tamper"):
        OrganizationSite.objects.filter(site_organization=scenario.a).update(logo_key=record.logo_key)
    assert find_site(scenario.a.slug).logo_key == record.logo_key
    assert Client(HTTP_HOST=host_of(scenario.a)).get("/site/logo/").status_code == 404  # Never B's bytes.


@pytest.mark.parametrize(
    "path",
    [
        "/",
        f"/tables/{NOTE}/",
        "/catalog/",
        "/notifications/",
        "/flows/tasks/",
        "/profile/",
        "/profile/notifications/",
        "/profile/privacy/",
        "/apps/example_notes/",
    ],
)
def test_no_page_on_the_host_says_skeleton(scenario, path):
    client = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)
    response = client.get(path)
    assert response.status_code == 200, path
    assert title(response).endswith("· Acme Pública") or title(response) == "Acme Pública"
    assert "Skeleton" not in response.content.decode().split("<body", 1)[1], path


def test_no_access_and_login_pages_say_the_organization(scenario):
    page = site_client(scenario.outsider, host_of(scenario.a), FIXTURE_PASSWORD).get("/")
    assert page.status_code == 404 and title(page) == "Você não tem acesso · Acme Pública"
    assert "Skeleton" not in page.content.decode()


def test_main_domain_keeps_skeleton_and_the_settings_brand(scenario):
    upload_logo(scenario.admin_a, scenario.a, PNG_A, 1)
    client = site_client(scenario.member, "testserver", FIXTURE_PASSWORD)
    home = client.get("/")
    assert title(home).endswith("Skeleton")
    assert "Identidade, organizações e registros protegidos." in home.content.decode()
    dashboard = client.get(f"/o/{scenario.a.pk}/").content.decode()
    assert f'src="/o/{scenario.a.pk}/settings/logo/"' in dashboard  # The members-only logo, as before.
    assert client.get(f"/o/{scenario.a.pk}/settings/logo/").content == PNG_A


def test_inside_the_tenant_on_the_host_the_logo_is_the_public_one(scenario, django_assert_max_num_queries):
    upload_logo(scenario.admin_a, scenario.a, PNG_A, 1)
    client = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD)
    html = client.get("/").content.decode()
    assert 'src="/site/logo/?v=' in html and "/settings/logo/" not in html


def test_anonymous_shell_does_not_read_tenant_settings(scenario):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as queries:
        Client(HTTP_HOST=host_of(scenario.a)).get("/accounts/login/")
    assert not [query for query in queries.captured_queries if "organizationsettings" in query["sql"]]
