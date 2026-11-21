"""The instance brand on every page (#598): title, header, footer, favicon, theme link and precedence."""

import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario
from tests.integration.appearance_support import acting, login, png, upload
from tests.site_support import enable_site, host_of, site_client, sites  # noqa: F401

pytestmark = [pytest.mark.usefixtures("skeleton_db", "live_appearance"), pytest.mark.needs_example_app]
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
NAME = "Acme Nuvem"


@pytest.fixture
def storage(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "skeleton_files": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "files"), "base_url": None},
        },
    }


@pytest.fixture
def scenario(storage):
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="shell")


def platform_user(scenario):
    """admin-a becomes the platform administrator (the first identity of the scenario is not)."""
    from skeleton.accounts.services import bootstrap_identity

    user, _ = bootstrap_identity(
        email="shell-platform@example.test", organization="shell-platform", name="P", password=FIXTURE_PASSWORD
    )
    return user


def set_brand(admin, *, name=NAME, footer="", logo=False, dark=False, theme=True):
    from skeleton.appearance.images import replace_image
    from skeleton.appearance.services import activate_theme, current_brand, revision_of, update_brand

    with acting(admin):
        update_brand(actor=admin, data={"product_name": name, "footer_text": footer}, expected_revision=0)
    if logo:
        with acting(admin):
            replace_image(actor=admin, kind="logo", upload=upload(png(64, 32), "logo.png"), expected_revision=1)
    if dark:
        with acting(admin):
            revision = revision_of(current_brand())
            replace_image(
                actor=admin, kind="logo_dark", upload=upload(png(64, 32), "logo.png"), expected_revision=revision
            )
    if theme:
        with acting(admin):
            activate_theme(actor=admin, builtin="esmeralda", expected_revision=revision_of(current_brand()))


def title(html):
    return TITLE.search(html).group(1).strip()


def test_without_a_brand_the_html_does_not_change(scenario):
    html = Client().get("/accounts/login/").content.decode()
    assert "/brand/" not in html
    assert title(html).endswith("· Skeleton")
    assert '<p class="font-medium">Skeleton</p>' in html
    assert "favicon.svg" in html


def test_the_name_replaces_skeleton_on_every_page(scenario, subtests):
    admin = platform_user(scenario)
    set_brand(admin, footer="Feito com cuidado")
    anonymous = Client()
    with subtests.test(page="login"):
        html = anonymous.get("/accounts/login/").content.decode()
        assert title(html).endswith(f"· {NAME}") and NAME in html and "Feito com cuidado" in html
        assert "Skeleton" not in html
    with subtests.test(page="signup and reset"):
        for path in ("/accounts/password/reset/", "/accounts/signup/"):
            html = anonymous.get(path).content.decode()
            assert "Skeleton" not in html, path
    with subtests.test(page="404"):
        response = anonymous.get("/nada-aqui/")
        assert response.status_code == 404
        html = response.content.decode()
        assert NAME in title(html) and "Skeleton" not in html and "/brand/theme.css" in html
    client = login(admin.email, FIXTURE_PASSWORD)
    pages = ["/", "/profile/", "/profile/privacy/", "/platform/", "/platform/users/", "/platform/organizations/"]
    for path in pages:
        with subtests.test(page=path):
            response = client.get(path)
            assert response.status_code == 200, path
            html = response.content.decode()
            assert NAME in title(html) and "Skeleton" not in html, path
            assert "/brand/theme.css?v=" in html


def test_the_theme_link_sits_between_the_core_css_and_the_organization_style(scenario):
    from skeleton.tenancy.organization_settings import update_settings

    admin = platform_user(scenario)
    set_brand(admin)
    with acting_as(scenario.admin_a, scenario.a) as actor:
        update_settings(
            actor=actor,
            data={"display_name": "Org A", "default_language": "", "default_timezone": "", "brand_color": "#9f1239"},
            expected_revision=0,
        )
    client = login(scenario.admin_a.email, FIXTURE_PASSWORD)
    html = client.get(f"/o/{scenario.a.pk}/").content.decode()
    core = html.index("skeleton/web/site.css")
    theme = html.index("/brand/theme.css?v=")
    style = html.index("<style nonce=")
    assert core < theme < style
    assert 'content="#9f1239"' in html  # theme-color of the organization wins.


def test_organization_brand_wins_and_an_organization_without_brand_inherits(scenario, subtests):
    from skeleton.tenancy.branding import replace_logo
    from skeleton.tenancy.organization_settings import update_settings

    admin = platform_user(scenario)
    set_brand(admin, logo=True)
    with acting_as(scenario.admin_a, scenario.a) as actor:
        update_settings(
            actor=actor,
            data={"display_name": "Org A", "default_language": "", "default_timezone": "", "brand_color": "#9f1239"},
            expected_revision=0,
        )
        replace_logo(actor=actor, upload=SimpleUploadedFile("logo.png", png(64, 32)), expected_revision=1)
    with subtests.test(organization="A, own brand"):
        html = login(scenario.admin_a.email, FIXTURE_PASSWORD).get(f"/o/{scenario.a.pk}/").content.decode()
        assert f"/o/{scenario.a.pk}/settings/logo/" in html and "/brand/logo/" not in html
        assert "Org A" in html and "--color-brand:#9f1239" in html
    with subtests.test(organization="B, no brand"):
        html = login(scenario.admin_b.email, FIXTURE_PASSWORD).get(f"/o/{scenario.b.pk}/").content.decode()
        assert "/brand/logo/?v=" in html and NAME in html and "/brand/theme.css?v=" in html
        assert "<style nonce=" not in html
        assert f"/o/{scenario.a.pk}/" not in html


def test_the_dark_logo_uses_a_picture_only_when_it_exists(scenario, subtests):
    admin = platform_user(scenario)
    set_brand(admin, logo=True, theme=False)
    with subtests.test(variant="light only"):
        html = Client().get("/accounts/login/").content.decode()
        assert "/brand/logo/?v=" in html and "<picture>" not in html
    from skeleton.appearance.images import replace_image
    from skeleton.appearance.services import current_brand, revision_of

    with acting(admin):
        revision = revision_of(current_brand())
        replace_image(actor=admin, kind="logo_dark", upload=upload(png(64, 32), "logo.png"), expected_revision=revision)
    with subtests.test(variant="with dark"):
        html = Client().get("/accounts/login/").content.decode()
        assert '<source media="(prefers-color-scheme: dark)" srcset="/brand/logo-dark/?v=' in html


def test_the_icon_is_the_favicon_below_the_organization_icon(scenario):
    from skeleton.appearance.images import replace_image

    admin = platform_user(scenario)
    with acting(admin):
        replace_image(actor=admin, kind="icon", upload=upload(png(), "icon.png"), expected_revision=0)
    html = Client().get("/accounts/login/").content.decode()
    assert '<link rel="icon" type="image/png" href="/brand/icon/?v=' in html and "favicon.svg" not in html


def test_an_organization_host_keeps_its_name_and_loads_the_instance_theme(scenario, sites):  # noqa: F811
    admin = platform_user(scenario)
    set_brand(admin)
    enable_site(scenario, scenario.a, display_name="Acme Pública")
    html = Client(HTTP_HOST=host_of(scenario.a)).get("/accounts/login/").content.decode()
    assert title(html).endswith("· Acme Pública") and NAME not in title(html)
    assert "/brand/theme.css?v=" in html
    client = site_client(scenario.admin_a, host_of(scenario.a), FIXTURE_PASSWORD)
    assert "Acme Pública" in title(client.get("/").content.decode())


def test_the_context_processor_adds_no_query_with_a_warm_cache(scenario):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    admin = platform_user(scenario)
    set_brand(admin)
    client = Client()
    client.get("/accounts/login/")  # Warms the cache of this process.
    with CaptureQueriesContext(connection) as queries:
        assert client.get("/accounts/login/").status_code == 200
    assert not [query for query in queries.captured_queries if "sk_appearance" in query["sql"]]


def test_the_500_page_renders_with_the_database_down(scenario, monkeypatch):
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.template.loader import render_to_string

    from skeleton.appearance.snapshot import current_appearance

    admin = platform_user(scenario)
    set_brand(admin)
    with acting(admin):
        current_appearance()  # Warm.

    def refuse(*args, **kwargs):
        raise AssertionError("no query while rendering the 500 page")

    monkeypatch.setattr(BaseDatabaseWrapper, "cursor", refuse)
    html = render_to_string("500.html")
    assert NAME in title(html) and "/brand/theme.css?v=" in html
