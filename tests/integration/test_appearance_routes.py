"""``/brand/`` (#597): the theme stylesheet and the instance images, public, cached, also on organization hosts."""

import pytest
from django.conf import settings
from django.test import Client

from tests.integration.appearance_support import acting, platform_admin, png, upload
from tests.site_support import enable_site, host_of, sites  # noqa: F401

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_appearance")
CUSTOM = ".card{outline:2px solid red}"


@pytest.fixture
def storage(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "skeleton_files": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "files"), "base_url": None},
        },
    }


def with_theme(admin):
    from skeleton.appearance.services import activate_theme, create_theme

    with acting(admin):
        theme = create_theme(actor=admin, data={"name": "Rota", "brand_color": "#047857", "custom_css": CUSTOM})
        activate_theme(actor=admin, theme_id=theme.pk, expected_revision=0)


def current():
    from skeleton.appearance.snapshot import current_appearance

    return current_appearance()


def test_without_a_theme_the_stylesheet_is_empty():
    response = Client().get("/brand/theme.css")
    assert response.status_code == 200 and response.content == b""
    assert response["Content-Type"] == "text/css; charset=utf-8"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "public, max-age=300"


def test_the_active_theme_headers_cache_and_safe_mode(subtests):
    admin = platform_admin()
    with_theme(admin)
    client = Client()
    full = client.get("/brand/theme.css")
    appearance = current()
    with subtests.test(case="full"):
        assert full.status_code == 200 and CUSTOM in full.content.decode()
        assert full["ETag"] == f'"{appearance.theme_version}"'
        assert full["Cache-Control"] == "public, max-age=300"
    with subtests.test(case="versioned"):
        response = client.get(f"/brand/theme.css?v={appearance.theme_version}")
        assert response["Cache-Control"] == "public, max-age=31536000, immutable"
        assert client.get("/brand/theme.css?v=outra")["Cache-Control"] == "public, max-age=300"
    with subtests.test(case="safe"):
        safe = client.get("/brand/theme.css?safe=1")
        assert CUSTOM not in safe.content.decode() and "--color-cobalt-600:#047857" in safe.content.decode()
        assert safe["ETag"] == f'"{appearance.theme_safe_version}"'
        versioned = client.get(f"/brand/theme.css?safe=1&v={appearance.theme_safe_version}")
        assert versioned["Cache-Control"] == "public, max-age=31536000, immutable"
    with subtests.test(case="304"):
        response = client.get("/brand/theme.css", HTTP_IF_NONE_MATCH=full["ETag"])
        assert response.status_code == 304 and response.content == b""
    with subtests.test(case="methods"):
        assert client.post("/brand/theme.css").status_code == 405
        assert client.head("/brand/theme.css").status_code == 200
        assert client.post("/brand/logo/").status_code == 405
    with subtests.test(case="warm cache"):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        client.get("/brand/theme.css")
        with CaptureQueriesContext(connection) as queries:
            assert client.get("/brand/theme.css").status_code == 200
        # Only the request transaction of the middleware: the stylesheet itself reads memory.
        assert not [query for query in queries.captured_queries if "sk_appearance" in query["sql"]]
        assert len(queries) <= 3


def test_images_are_served_only_when_they_exist(storage, subtests):
    from skeleton.appearance.images import replace_image

    admin = platform_admin()
    with subtests.test(case="none"):
        for path in ("/brand/logo/", "/brand/logo-dark/", "/brand/icon/"):
            assert Client().get(path).status_code == 404
    icon = png()
    with acting(admin):
        replace_image(actor=admin, kind="icon", upload=upload(icon, "icon.png"), expected_revision=0)
    with subtests.test(case="icon"):
        response = Client().get("/brand/icon/?v=x&key=org/../etc")
        assert response.status_code == 200 and response.content == icon
        assert response["Content-Type"] == "image/png"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Content-Security-Policy"] == "default-src 'none'; sandbox"
        assert response["Cache-Control"] == "public, max-age=300"
    with subtests.test(case="logo still missing"):
        assert Client().get("/brand/logo/").status_code == 404


@pytest.mark.needs_example_app
def test_organization_hosts_load_the_instance_theme(sites):  # noqa: F811
    from examples.notes.models import Note
    from skeleton.testing.service_api import build_service_scenario

    scenario = build_service_scenario(Note, slug="brandhost")
    enable_site(scenario, scenario.a, display_name="Acme")
    with_theme(platform_admin())
    client = Client(HTTP_HOST=host_of(scenario.a))
    response = client.get("/brand/theme.css")
    assert response.status_code == 200 and CUSTOM in response.content.decode()
    assert client.get("/platform/").status_code == 404


def test_an_app_cannot_declare_the_brand_root(subtests):
    from django.core.exceptions import ValidationError
    from django.urls import path, re_path

    from skeleton.system.presentation import RESERVED_ROOTS, _validate_reserved_routes

    assert "brand" in RESERVED_ROOTS
    for route in (path("brand/", lambda r: None), path("brand/x/", lambda r: None), path("<slug:x>/", lambda r: None)):
        with subtests.test(route=str(route.pattern)), pytest.raises(ValidationError):
            _validate_reserved_routes([route])
    with subtests.test(route="regex"), pytest.raises(ValidationError):
        _validate_reserved_routes([re_path(r"^brand/theme", lambda r: None)])


def test_the_global_csp_is_unchanged():
    response = Client().get("/brand/theme.css")
    header = "Content-Security-Policy" if settings.SECURE_CSP else "Content-Security-Policy-Report-Only"
    policy = response[header]
    assert "default-src 'self'" in policy and "img-src 'self' data:" in policy and "font-src 'self'" in policy
    assert "'unsafe-inline'" not in policy


# --------------------------------------------------------------------------- manifest and favicon (#639)


def test_the_manifest_of_the_main_domain(storage, subtests):
    import json

    from skeleton.appearance.images import replace_image
    from skeleton.appearance.services import activate_theme, current_brand, revision_of, update_brand
    from skeleton.appearance.snapshot import reset_appearance_cache

    client = Client()
    with subtests.test(case="without a brand"):
        response = client.get("/brand/manifest.webmanifest")
        assert response.status_code == 200 and response["Content-Type"] == "application/manifest+json"
        data = json.loads(response.content)
        assert data["name"] == "Skeleton" and data["icons"][0]["type"] == "image/svg+xml"
        assert "favicon.svg" in client.get("/favicon.ico")["Location"]
    admin = platform_admin()
    with acting(admin):
        update_brand(actor=admin, data={"product_name": "Acme Nuvem"}, expected_revision=0)
        replace_image(actor=admin, kind="icon", upload=upload(png()), expected_revision=1)
        activate_theme(actor=admin, builtin="vinho", expected_revision=revision_of(current_brand()))
    reset_appearance_cache()
    with subtests.test(case="with name, colour and icon"):
        data = json.loads(client.get("/brand/manifest.webmanifest").content)
        assert data["name"] == "Acme Nuvem" and data["short_name"] == "Acme Nuvem"
        assert data["theme_color"] == "#9f1239" and data["icons"][0]["src"].startswith("/brand/icon/?v=")
        assert client.get("/favicon.ico")["Location"].startswith("/brand/icon/?v=")
        assert (
            '<link rel="manifest" href="/brand/manifest.webmanifest">'
            in client.get("/accounts/login/").content.decode()
        )


@pytest.mark.needs_example_app
def test_an_organization_host_keeps_its_own_manifest(sites):  # noqa: F811
    from examples.notes.models import Note
    from skeleton.testing.service_api import build_service_scenario

    scenario = build_service_scenario(Note, slug="manifesthost")
    enable_site(scenario, scenario.a, display_name="Acme")
    client = Client(HTTP_HOST=host_of(scenario.a))
    assert client.get("/brand/manifest.webmanifest").status_code == 404
