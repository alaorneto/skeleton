"""The active theme package on the pages (#636): fonts, logos (also SVG), icon, name, footer and precedence."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client

from skeleton.tenancy.context import system_context
from tests.integration.appearance_support import acting, login, member, platform_admin, png, upload
from tests.theme_package_support import FONT, LOGO_SVG, files, make_zip, manifest

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_appearance")


@pytest.fixture
def storage(settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "skeleton_files": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "files"), "base_url": None},
        },
    }


def install_and_activate(admin, data=None, *, activate=True):
    from skeleton.appearance.packages import install_theme_package
    from skeleton.appearance.services import activate_theme, current_brand, revision_of

    with acting(admin):
        theme = install_theme_package(actor=admin, data=data or make_zip(files())).theme
        if activate:
            activate_theme(actor=admin, theme_id=theme.pk, expected_revision=revision_of(current_brand()))
    return theme


def appearance():
    from skeleton.appearance.snapshot import current_appearance, reset_appearance_cache

    reset_appearance_cache()
    return current_appearance()


def test_the_stylesheet_declares_the_fonts_and_the_family(storage):
    install_and_activate(platform_admin())
    current = appearance()
    ref = current.theme_asset("aurora-sans.woff2")
    face = f'src:url("/brand/fonts/aurora-sans.woff2?v={ref.version}") format("woff2")'
    for css in (current.theme_css, current.theme_css_tokens):  # Fonts are generated, also in safe mode.
        assert '@font-face{font-family:"Aurora Sans";' in css and face in css
        assert '--font-sans:"Aurora Sans","IBM Plex Sans Variable"' in css
    client = Client()
    assert face in client.get("/brand/theme.css").content.decode()
    assert face in client.get("/brand/theme.css?safe=1").content.decode()


def test_fonts_are_served_only_when_declared(storage, subtests):
    install_and_activate(platform_admin())
    ref = appearance().theme_asset("aurora-sans.woff2")
    client = Client()
    response = client.get(f"/brand/fonts/aurora-sans.woff2?v={ref.version}")
    assert response.status_code == 200 and response.content == FONT
    assert response["Content-Type"] == "font/woff2" and response["X-Content-Type-Options"] == "nosniff"
    assert "immutable" in response["Cache-Control"]
    assert "immutable" not in client.get("/brand/fonts/aurora-sans.woff2")["Cache-Control"]
    for name in ("outra.woff2", "aurora-sans.otf", "..%2Ftheme.css", "Aurora.woff2"):
        with subtests.test(name=name):
            assert client.get(f"/brand/fonts/{name}").status_code == 404


def test_theme_images_fill_what_the_brand_left_empty(storage, subtests):
    from skeleton.appearance.images import replace_image
    from skeleton.appearance.services import current_brand, revision_of

    admin = platform_admin()
    install_and_activate(admin)
    client = Client()
    with subtests.test(case="svg logo of the theme"):
        response = client.get("/brand/logo/")
        assert response.status_code == 200 and response.content == LOGO_SVG.encode()
        assert response["Content-Type"] == "image/svg+xml" and response["X-Content-Type-Options"] == "nosniff"
        assert "sandbox" in response["Content-Security-Policy"]
        assert "default-src 'none'" in response["Content-Security-Policy"]
        assert client.get("/brand/logo-dark/")["Content-Type"] == "image/png"
        assert client.get("/brand/icon/")["Content-Type"] == "image/png"
        assert appearance().logo_source == "theme" and appearance().icon_source == "theme"
    with subtests.test(case="the uploaded logo wins, with its own dark variant only"):
        with acting(admin):
            replace_image(
                actor=admin,
                kind="logo",
                upload=upload(png(64, 32), "l.png"),
                expected_revision=revision_of(current_brand()),
            )
        current = appearance()
        assert current.logo_source == "brand" and current.logo_dark_version == ""  # Never two logos mixed.
        assert client.get("/brand/logo/")["Content-Type"] == "image/png"
        assert client.get("/brand/logo-dark/").status_code == 404
        assert current.icon_source == "theme"


def test_name_and_footer_of_the_theme_fill_the_brand(storage):
    from skeleton.appearance.services import current_brand, revision_of, update_brand

    admin = platform_admin()
    install_and_activate(admin)
    current = appearance()
    assert (current.product_name, current.footer_text) == ("Aurora", "Aurora · atendimento ao cliente")
    page = Client().get("/accounts/login/").content.decode()
    assert "Aurora" in page and "Aurora · atendimento ao cliente" in page and "Skeleton" not in page
    with acting(admin):
        update_brand(actor=admin, data={"product_name": "Acme"}, expected_revision=revision_of(current_brand()))
    current = appearance()
    assert current.product_name == "Acme" and current.footer_text == "Aurora · atendimento ao cliente"


def test_form_and_builtin_themes_are_unchanged(storage):
    from skeleton.appearance.services import activate_theme, create_theme, current_brand, revision_of
    from skeleton.appearance.themes import BUILTIN_THEMES, ThemeValues, theme_css

    admin = platform_admin()
    with acting(admin):
        theme = create_theme(actor=admin, data={"name": "Mar", "brand_color": "#0f766e", "radius": "round"})
        activate_theme(actor=admin, theme_id=theme.pk, expected_revision=0)
    current = appearance()
    assert current.theme_css == theme_css(ThemeValues("Mar", "#0f766e", "round"))
    assert current.theme_assets == () and current.logo_source == ""
    with acting(admin):
        activate_theme(actor=admin, builtin="vinho", expected_revision=revision_of(current_brand()))
    assert appearance().theme_css == theme_css(BUILTIN_THEMES["vinho"])


def test_email_takes_a_raster_logo_of_the_theme_never_an_svg(storage):
    from skeleton.tenancy.email_brand import email_brand

    admin = platform_admin()
    install_and_activate(admin)
    appearance()
    with system_context(reason="test:email-theme"):
        values = email_brand()
    assert values["name"] == "Aurora" and values["logo"] is None  # The theme logo is SVG.
    raster = manifest(id="org.example.theme.raster", name="Raster", images={"logo": "images/logo-dark.png"})
    install_and_activate(
        admin, make_zip(files({"images/logo.svg": None, "images/icon.png": None}, manifest_value=raster))
    )
    appearance()
    with system_context(reason="test:email-theme"):
        values = email_brand()
    assert values["logo"] is not None and values["logo"][1] == "png"


def test_preview_of_a_package_theme_only_for_the_administrator(storage):
    admin = platform_admin()
    theme = install_and_activate(admin, activate=False)
    client = login(admin.email)
    page = client.get(f"/platform/appearance/themes/{theme.pk}/preview/").content.decode()
    assert f"/platform/appearance/themes/{theme.pk}/preview/images/logo/" in page
    css = client.get(f"/platform/appearance/themes/{theme.pk}/preview.css").content.decode()
    font = f"/platform/appearance/themes/{theme.pk}/preview/fonts/aurora-sans.woff2"
    assert font in css
    response = client.get(font)
    assert response.status_code == 200
    assert "private" in response["Cache-Control"] and "no-store" in response["Cache-Control"]
    assert client.get(f"/platform/appearance/themes/{theme.pk}/preview/images/logo/").status_code == 200
    assert client.get(f"/platform/appearance/themes/{theme.pk}/preview/images/icon/").status_code == 404
    assert Client().get("/brand/fonts/aurora-sans.woff2").status_code == 404  # Not active: not public.
    other = login(member().email)
    assert other.get(font).status_code in (302, 403, 404)


def test_show_says_where_each_value_comes_from(storage):
    install_and_activate(platform_admin())
    out = StringIO()
    call_command("appearance", "show", stdout=out)
    text = out.getvalue()
    assert "Nome do produto: Aurora (do tema)" in text
    assert "Tema ativo: pacote: Aurora (org.example.theme.aurora 1.0.0)" in text
    assert "Logo: sim (do tema)" in text and "Ícone: sim (do tema)" in text
    assert "platform/branding" not in text
