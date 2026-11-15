"""Favicon, organization icon and web manifest (#485)."""

import io
import struct
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests import support
from tests.site_support import enable_site, host_of, site_client, sites  # noqa: F401

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
FAVICON = "/static/skeleton/web/favicon.svg"


def png(fill=b"\x00", width=512, height=512):
    head = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    return head + fill * 200


@pytest.fixture
def scenario(sites):  # noqa: F811
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="icon")
    enable_site(scenario, scenario.a, display_name="Acme", brand_color="#0f766e")
    enable_site(scenario, scenario.b)
    return scenario


def settings_row(organization):
    from skeleton.tenancy.models import OrganizationSettings

    with system_context(reason="test:icon-row", organization_id=organization.pk):
        return OrganizationSettings.objects.get()


def upload(client, organization, content, revision, name="icon.png"):
    url = reverse("web:organization-icon-update", args=[organization.pk])
    data = {
        "action": "upload",
        "revision": str(revision),
        "icon": SimpleUploadedFile(name, content),
        "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
    }
    return client.post(url, data)


def remove(client, organization, revision):
    url = reverse("web:organization-icon-update", args=[organization.pk])
    data = {"action": "remove", "revision": str(revision), "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
    return client.post(url, data)


def login(user):
    return support.login(user, password=FIXTURE_PASSWORD)


def test_default_favicon_on_the_main_domain(scenario):
    client = login(scenario.member)
    for path in ("/", f"/o/{scenario.a.pk}/", "/accounts/email/"):
        html = client.get(path).content.decode()
        assert f'<link rel="icon" type="image/svg+xml" href="{FAVICON}">' in html, path
        assert 'rel="manifest"' not in html
    response = Client().get("/favicon.ico")
    assert (response.status_code, response["Location"]) == (302, FAVICON)


def test_icon_upload_appears_on_both_hosts_and_removal_restores_the_default(scenario):
    admin = login(scenario.admin_a)
    assert upload(admin, scenario.a, png(), 1).status_code == 302
    version = settings_row(scenario.a).icon_key[-12:]
    login_page = Client(HTTP_HOST=host_of(scenario.a)).get("/accounts/login/").content.decode()
    assert f'<link rel="icon" type="image/png" href="/site/icon/?v={version}">' in login_page
    assert '<link rel="manifest" href="/site/manifest.webmanifest">' in login_page
    dashboard = admin.get(f"/o/{scenario.a.pk}/").content.decode()
    assert f'href="/o/{scenario.a.pk}/settings/icon/?v={version}"' in dashboard
    assert admin.get(f"/o/{scenario.a.pk}/settings/icon/").content == png()
    public = Client(HTTP_HOST=host_of(scenario.a)).get("/site/icon/")
    assert public.content == png() and public["Cache-Control"] == "public, max-age=300"
    assert public["X-Content-Type-Options"] == "nosniff"
    favicon = Client(HTTP_HOST=host_of(scenario.a)).get("/favicon.ico")
    assert favicon["Location"] == f"/site/icon/?v={version}"
    assert upload(admin, scenario.a, png(b"\x01"), 2).status_code == 302  # A new icon changes ?v=.
    assert settings_row(scenario.a).icon_key[-12:] != version
    assert remove(admin, scenario.a, 3).status_code == 302
    assert Client(HTTP_HOST=host_of(scenario.a)).get("/site/icon/")["Location"] == FAVICON
    assert f'href="{FAVICON}"' in admin.get(f"/o/{scenario.a.pk}/").content.decode()


@pytest.mark.parametrize(
    ("content", "name"),
    [
        (b"\xff\xd8\xff" + b"\x00" * 200, "icon.jpg"),
        (png(width=256, height=256), "icon.png"),
        (png(width=512, height=400), "icon.png"),
        (png() + b"\x00" * (300 * 1024), "icon.png"),
        (b"\xff\xd8\xff" + b"\x00" * 200, "icon.png"),
    ],
)
def test_invalid_icons_are_refused(scenario, content, name):
    admin = login(scenario.admin_a)
    assert upload(admin, scenario.a, content, 1, name=name).status_code == 400
    assert settings_row(scenario.a).icon_key == ""


def test_stale_revision_is_refused(scenario):
    assert upload(login(scenario.admin_a), scenario.a, png(), 0).status_code == 409


def test_public_routes_never_mix_organizations(scenario):
    admin_a, admin_b = login(scenario.admin_a), login(scenario.admin_b)
    upload(admin_a, scenario.a, png(b"\x0a"), 1)
    upload(admin_b, scenario.b, png(b"\x0b"), 1)
    assert Client(HTTP_HOST=host_of(scenario.a)).get("/site/icon/").content == png(b"\x0a")
    assert Client(HTTP_HOST=host_of(scenario.b)).get("/site/icon/").content == png(b"\x0b")
    assert upload(admin_a, scenario.b, png(), 2).status_code == 404  # A's administrator cannot change B.
    for path in ("/site/icon/", "/site/manifest.webmanifest"):
        assert Client().get(path).status_code == 404  # Main domain.


def test_manifest(scenario):
    response = Client(HTTP_HOST=host_of(scenario.a)).get("/site/manifest.webmanifest")
    assert response["Content-Type"] == "application/manifest+json"
    assert response.json() == {
        "name": "Acme",
        "short_name": "Acme",
        "start_url": "/",
        "display": "standalone",
        "theme_color": "#0f766e",
    }
    upload(login(scenario.admin_a), scenario.a, png(), 1)
    data = Client(HTTP_HOST=host_of(scenario.a)).get("/site/manifest.webmanifest").json()
    assert data["icons"] == [{"src": "/site/icon/", "sizes": "512x512", "type": "image/png"}]
    other = Client(HTTP_HOST=host_of(scenario.b)).get("/site/manifest.webmanifest").json()
    assert other["name"] == "Service contract B" and "icons" not in other


def test_orphan_job_keeps_the_icon_and_removes_a_replaced_one(scenario):
    from skeleton.files.orphans import orphan_keys
    from skeleton.files.storage import put_stream
    from skeleton.tenancy.branding import icon_key

    upload(login(scenario.admin_a), scenario.a, png(), 1)
    current = settings_row(scenario.a).icon_key
    stale = icon_key(scenario.a.pk)
    put_stream(stale, io.BytesIO(png()), max_bytes=len(png()))
    later = timezone.now() + timedelta(days=1)
    with system_context(reason="test:icon-orphans", organization_id=scenario.a.pk):
        found = orphan_keys(scenario.a.pk, now=later)
    assert stale in found and current not in found


def test_index_follows_the_icon(scenario):
    from skeleton.tenancy.site import find_site

    upload(login(scenario.admin_a), scenario.a, png(), 1)
    assert find_site(scenario.a.slug).icon_key == settings_row(scenario.a).icon_key != ""
    page = site_client(scenario.member, host_of(scenario.a), FIXTURE_PASSWORD).get("/").content.decode()
    assert 'rel="icon" type="image/png" href="/site/icon/?v=' in page
