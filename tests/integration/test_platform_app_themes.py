"""Themes of apps on the platform pages (#644): catalog, preview, orphan alert, app page and uninstall."""

from uuid import uuid4

import pytest
from django.urls import reverse

from tests.integration.appearance_support import PASSWORD, acting, login, member, platform_admin
from tests.theme_package_support import register_theme_app, unregister_theme_app

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_appearance")
KEY = "theme_demo:aurora"


@pytest.fixture
def theme_app(tmp_path):
    from skeleton.appearance.app_themes import reset_app_themes_cache

    reset_app_themes_cache()
    register_theme_app(tmp_path)
    yield tmp_path
    unregister_theme_app()


@pytest.fixture
def admin_client(theme_app):
    admin = platform_admin()
    return admin, login(admin.email)


def post(client, url, data=None):
    return client.post(url, {**(data or {}), "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


def brand():
    from skeleton.appearance.services import current_brand

    return current_brand()


def test_the_catalog_lists_app_themes_with_only_preview_and_activate(admin_client):
    admin, client = admin_client
    html = client.get(reverse("platform:appearance")).content.decode()
    assert "Temas de apps" in html and "Da app Demo de temas · v1.2.0" in html
    assert "Sai do catálogo quando a app for desinstalada." in html
    assert reverse("platform:appearance-app-preview", args=["theme_demo", "aurora"]) in html
    assert f'value="{KEY}"' in html
    response = post(client, reverse("platform:appearance-activate"), {"app_theme": KEY, "revision": 0})
    assert response.status_code == 302 and brand().app_theme == KEY
    html = client.get(reverse("platform:appearance")).content.decode()
    assert f'value="{KEY}"' not in html  # Active: no "Ativar".
    response = post(client, reverse("platform:appearance-activate"), {"app_theme": "theme_demo:nada", "revision": 1})
    assert response.status_code == 400


def test_preview_of_an_app_theme_only_for_the_administrator(admin_client, subtests):
    admin, client = admin_client
    base = "/platform/appearance/apps/theme_demo/aurora/"
    page = client.get(base + "preview/").content.decode()
    assert "Sobre este pacote" in page and "Demo de temas · v1.2.0" in page
    css = client.get(base + "preview.css")
    assert (
        css.status_code == 200
        and "no-store" in css["Cache-Control"]
        and base + "preview/fonts/aurora-sans.woff2" in css.content.decode()
    )
    assert client.get(base + "preview/fonts/aurora-sans.woff2").status_code == 200
    assert client.get(base + "preview/images/logo/").status_code == 200
    with subtests.test(case="unknown"):
        assert client.get("/platform/appearance/apps/theme_demo/nada/preview/").status_code == 404
        assert client.get(base + "preview/images/icon/").status_code == 404
    with subtests.test(case="limited role"):
        other = login(member().email, PASSWORD)
        assert other.get(base + "preview/").status_code == 403


def test_an_orphan_active_theme_is_announced(admin_client):
    from skeleton.appearance.app_themes import reset_app_themes_cache
    from skeleton.appearance.services import activate_theme

    admin, client = admin_client
    with acting(admin):
        activate_theme(actor=admin, app_theme=KEY, expected_revision=0)
    unregister_theme_app()
    reset_app_themes_cache()
    html = client.get(reverse("platform:appearance")).content.decode()
    assert "O tema ativo vinha da app theme_demo, que não está mais instalada." in html
    assert "Temas de apps" not in html


def test_the_app_page_lists_its_themes(admin_client):
    from skeleton.appearance.services import activate_theme

    admin, client = admin_client
    with acting(admin):
        activate_theme(actor=admin, app_theme=KEY, expected_revision=0)
    html = client.get(reverse("platform:app", args=["theme_demo"])).content.decode()
    assert "Temas da instância" in html and "Aurora" in html and "ativo" in html


def test_the_uninstall_preview_warns_about_the_themes(admin_client, monkeypatch):
    from skeleton.appearance.services import activate_theme
    from skeleton.web.platform import updates

    admin, client = admin_client
    plan = {"state": "ready", "plan": {}, "request": {"action": "uninstall", "app_ids": ["org.example.demo"]}}
    monkeypatch.setattr(updates, "preview_status", lambda **kwargs: plan)
    url = reverse("platform:updates-preview", args=[uuid4()])
    html = client.get(url).content.decode()
    assert "A app Demo de temas traz temas da instância (Aurora)." in html
    assert "O tema ativo da instância" not in html
    with acting(admin):
        activate_theme(actor=admin, app_theme=KEY, expected_revision=0)
    html = client.get(url).content.decode()
    assert "O tema ativo da instância (Aurora) vem desta app." in html
    assert updates.uninstall_theme_warnings({"action": "update", "app_ids": ["org.example.demo"]}) == []
