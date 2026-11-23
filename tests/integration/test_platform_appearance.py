"""``/platform/appearance/`` (#599): access, the full flow, negatives, CAS, reauthentication and the preview."""

import logging
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from tests.integration.appearance_support import PASSWORD, acting, login, member, platform_admin, png

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_appearance")
HIDE_ALL = "body{display:none}"


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
def admin_client(storage):
    admin = platform_admin()
    return admin, login(admin.email)


def post(client, url, data=None, **extra):
    return client.post(url, {**(data or {}), "csrfmiddlewaretoken": client.cookies["csrftoken"].value}, **extra)


def brand():
    from skeleton.appearance.services import current_brand

    return current_brand()


def revision():
    from skeleton.appearance.services import revision_of

    return revision_of(brand())


def theme_named(name):
    from skeleton.appearance.models import InstanceTheme

    return InstanceTheme.objects.filter(name=name).first()


def test_access(admin_client, subtests):
    from skeleton.accounts.platform import grant_platform_role
    from skeleton.tenancy.context import request_context

    admin, client = admin_client
    url = reverse("platform:appearance")
    with subtests.test(who="administrator"):
        response = client.get(url)
        assert response.status_code == 200
        assert "/brand/theme.css" not in response.content.decode()  # No theme yet: no stylesheet link.
    with subtests.test(who="anonymous"):
        response = Client().get(url)
        assert response.status_code == 302 and reverse("account_login") in response["Location"]
    auditor = member("appearance-auditor@example.test")
    with request_context(actor=admin, request_id=uuid4(), source="test:appearance-roles"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    for who, email in (("member", member().email), ("auditor", auditor.email)):
        with subtests.test(who=who):
            other = login(email, PASSWORD)
            assert other.get(url).status_code == 403
            assert (
                post(other, reverse("platform:appearance-brand"), {"product_name": "X", "revision": 0}).status_code
                == 403
            )
            assert other.get(reverse("platform:appearance-builtin-preview-css", args=["vinho"])).status_code == 403


def test_full_flow_through_the_interface(admin_client, caplog):
    from skeleton.appearance.snapshot import current_appearance

    admin, client = admin_client
    caplog.set_level(logging.INFO, logger="skeleton.security")
    response = post(
        client, reverse("platform:appearance-brand"), {"product_name": "Acme", "footer_text": "Rodapé", "revision": 0}
    )
    assert response.status_code == 302
    for kind, content, name in (
        ("logo", png(64, 32), "logo.png"),
        ("logo_dark", png(64, 32), "dark.png"),
        ("icon", png(), "icon.png"),
    ):
        response = post(
            client,
            reverse("platform:appearance-image", args=[kind]),
            {"action": "upload", "revision": revision(), "image": SimpleUploadedFile(name, content)},
        )
        assert response.status_code == 302, kind
    assert brand().logo_key and brand().logo_dark_key and brand().icon_key
    response = post(
        client, reverse("platform:appearance-image", args=["logo_dark"]), {"action": "remove", "revision": revision()}
    )
    assert response.status_code == 302 and brand().logo_dark_key == ""
    data = {"name": "Mar", "brand_color": "#0F766E", "radius": "round", "font": "serif", "custom_css": HIDE_ALL}
    assert post(client, reverse("platform:appearance-theme-create"), data).status_code == 302
    theme = theme_named("Mar")
    edit = reverse("platform:appearance-theme", args=[theme.pk])
    assert client.get(edit).status_code == 200
    assert post(client, edit, {**data, "radius": "sharp", "revision": theme.revision}).status_code == 302
    preview = client.get(reverse("platform:appearance-theme-preview", args=[theme.pk])).content.decode()
    css_url = reverse("platform:appearance-theme-preview-css", args=[theme.pk])
    assert css_url in preview and "/brand/theme.css" not in preview
    css = client.get(css_url)
    assert HIDE_ALL in css.content.decode()
    assert "private" in css["Cache-Control"] and "no-store" in css["Cache-Control"]
    assert "public" not in css["Cache-Control"]
    assert css["Content-Type"] == "text/css; charset=utf-8"
    assert current_appearance().theme_css == ""  # The preview did not activate anything.
    response = post(client, reverse("platform:appearance-activate"), {"theme": str(theme.pk), "revision": revision()})
    assert response.status_code == 302
    with acting(admin):
        assert HIDE_ALL in current_appearance().theme_css
    page = client.get(reverse("platform:appearance")).content.decode()
    assert "/brand/theme.css?v=" in page and "safe=1" in page  # The safe stylesheet: the page stays usable.
    assert "badge badge-success" in page
    assert HIDE_ALL not in client.get("/brand/theme.css?safe=1").content.decode()
    response = post(client, reverse("platform:appearance-activate"), {"builtin": "vinho", "revision": revision()})
    assert response.status_code == 302 and brand().builtin_theme == "vinho"
    delete = reverse("platform:appearance-theme-delete", args=[theme.pk])
    assert client.get(delete).status_code == 200
    assert post(client, delete, {"revision": theme_named("Mar").revision}).status_code == 302
    assert theme_named("Mar") is None
    names = [getattr(record, "event", "") for record in caplog.records if record.name == "skeleton.security"]
    for name in (
        "appearance.brand_changed",
        "appearance.image_changed",
        "appearance.theme_changed",
        "appearance.theme_activated",
    ):
        assert name in names
    logged = repr([record.__dict__ for record in caplog.records if record.name == "skeleton.security"])
    assert "Acme" not in logged and HIDE_ALL not in logged and "platform/branding" not in logged
    from skeleton.audit.models import AuditEvent

    with acting(admin):
        assert AuditEvent.objects.filter(model_label="sk_appearance.instancebrand").count() >= 6
        assert AuditEvent.objects.filter(model_label="sk_appearance.instancetheme").count() == 3


def test_negatives(admin_client, subtests):
    admin, client = admin_client
    create = reverse("platform:appearance-theme-create")
    with subtests.test(case="no csrf"):
        assert (
            client.post(reverse("platform:appearance-brand"), {"product_name": "X", "revision": 0}).status_code == 403
        )
    with subtests.test(case="unknown field"):
        response = post(
            client, reverse("platform:appearance-brand"), {"product_name": "X", "revision": 0, "logo_key": "x"}
        )
        assert response.status_code == 400 and brand() is None
        assert (
            post(client, create, {"name": "T", "radius": "default", "font": "default", "sysid": "x"}).status_code == 400
        )
    cases = [
        ({"name": "Cor", "brand_color": "#ffff00", "radius": "default", "font": "default"}, "Contraste"),
        ({"name": "Css", "radius": "default", "font": "default", "custom_css": "@import 'x';"}, "@import"),
        ({"name": "Url", "radius": "default", "font": "default", "custom_css": ".a{background:url(//x)}"}, "url("),
        # #631: the message names the line, also for a < inside a comment.
        ({"name": "Lt", "radius": "default", "font": "default", "custom_css": "a{}\n/* <style> */"}, "Linha 2:"),
    ]
    for data, message in cases:
        with subtests.test(case=message):
            response = post(client, create, data)
            assert response.status_code == 400 and message in response.content.decode()
            assert theme_named(data["name"]) is None
    with subtests.test(case="bad image"):
        response = post(
            client,
            reverse("platform:appearance-image", args=["icon"]),
            {"action": "upload", "revision": 0, "image": SimpleUploadedFile("icon.png", png(64, 64))},
        )
        assert response.status_code == 400 and "512" in response.content.decode()
    with subtests.test(case="unknown kind"):
        assert (
            post(
                client, reverse("platform:appearance-image", args=["banner"]), {"action": "remove", "revision": 0}
            ).status_code
            == 404
        )


def test_cas_and_the_active_theme(admin_client, subtests):
    admin, client = admin_client
    assert post(client, reverse("platform:appearance-brand"), {"product_name": "Um", "revision": 0}).status_code == 302
    with subtests.test(case="old revision"):
        response = post(client, reverse("platform:appearance-brand"), {"product_name": "Dois", "revision": 0})
        assert response.status_code == 409 and brand().product_name == "Um"
    post(client, reverse("platform:appearance-theme-create"), {"name": "Ativo", "radius": "default", "font": "default"})
    theme = theme_named("Ativo")
    post(client, reverse("platform:appearance-activate"), {"theme": str(theme.pk), "revision": revision()})
    with subtests.test(case="delete active"):
        response = post(
            client, reverse("platform:appearance-theme-delete", args=[theme.pk]), {"revision": theme.revision}
        )
        assert response.status_code == 400 and "tema ativo" in response.content.decode()
        assert theme_named("Ativo") is not None
    with subtests.test(case="old theme revision"):
        edit = reverse("platform:appearance-theme", args=[theme.pk])
        data = {"name": "Novo", "radius": "default", "font": "default", "revision": theme.revision + 5}
        assert post(client, edit, data).status_code == 409


def test_writes_require_a_recent_authentication(admin_client):
    admin, client = admin_client
    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = post(client, reverse("platform:appearance-brand"), {"product_name": "Tarde", "revision": 0})
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    assert brand() is None
    assert client.get(reverse("platform:appearance-theme-create")).status_code == 200  # The form still renders.


def test_works_without_javascript_and_sidebar_item(admin_client):
    admin, client = admin_client
    html = client.get(reverse("platform:appearance")).content.decode()
    assert f'href="{reverse("platform:appearance")}" aria-current="page"' in html
    assert "<script>" not in html.split("</head>", 1)[1]
    assert 'enctype="multipart/form-data"' in html


def test_the_legacy_font_name_reads_and_saves_as_the_default(admin_client):
    """#690: "inter" (the core font's name before) is accepted and stored as "default"."""
    from skeleton.appearance.services import create_theme

    admin, client = admin_client
    with acting(admin):
        theme = create_theme(actor=admin, data={"name": "Antigo", "radius": "default", "font": "inter"})
    assert theme.font == "default"
    page = client.get(reverse("platform:appearance-theme", args=[theme.pk])).content.decode()
    assert 'value="default" selected' in page or 'value="default" checked' in page
    assert "IBM Plex Sans (padrão)" in page
