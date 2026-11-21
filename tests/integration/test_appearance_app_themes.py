"""Themes shipped by installed apps (#643): registry in memory, activation, files, orphan key and refusals."""

from io import StringIO

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client

from tests.integration.appearance_support import acting, platform_admin
from tests.theme_package_support import (
    FONT,
    LOGO_SVG,
    app_theme_manifest,
    files,
    make_zip,
    manifest,
    register_theme_app,
    unregister_theme_app,
)

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_appearance")
KEY = "theme_demo:aurora"


@pytest.fixture
def theme_app(tmp_path):
    from skeleton.appearance.app_themes import reset_app_themes_cache

    reset_app_themes_cache()
    register_theme_app(tmp_path)
    yield tmp_path
    unregister_theme_app()


def fresh():
    from skeleton.appearance.snapshot import current_appearance, reset_appearance_cache

    reset_appearance_cache()
    return current_appearance()


def activate(admin, **kwargs):
    from skeleton.appearance.services import activate_theme, current_brand, revision_of

    with acting(admin):
        return activate_theme(actor=admin, expected_revision=revision_of(current_brand()), **kwargs)


def test_the_registry_lists_valid_themes_without_bytes(theme_app, tmp_path, django_assert_num_queries):
    from skeleton.appearance.app_themes import app_themes, get_app_theme, reset_app_themes_cache

    reset_app_themes_cache()
    with django_assert_num_queries(0):
        themes = app_themes()
    theme = themes[KEY]
    assert (theme.app_label, theme.slug, theme.app_version, theme.package.name) == (
        "theme_demo",
        "aurora",
        "1.2.0",
        "Aurora",
    )
    assert all(image.content == b"" for image in theme.package.images) and theme.package.fonts[0].content == b""
    assert get_app_theme("theme_demo:nada") is None and get_app_theme("") is None
    unregister_theme_app()
    register_theme_app(
        tmp_path / "bad", themes={"aurora": files(manifest_value=app_theme_manifest(tokens={"brand_color": "#ffff00"}))}
    )
    reset_app_themes_cache()
    assert app_themes() == {}  # An invalid theme stays out (E047 reports it).


def test_activation_applies_everything_of_the_app_theme(theme_app):
    admin = platform_admin()
    activate(admin, app_theme=KEY)
    current = fresh()
    assert current.brand_color == "#1d4ed8" and "--acme-accent" in current.theme_css
    assert '@font-face{font-family:"Aurora Sans"' in current.theme_css
    assert (current.product_name, current.logo_source, current.icon_source) == ("Aurora", "theme", "theme")
    client = Client()
    assert client.get("/brand/logo/").content == LOGO_SVG.encode()
    font = current.theme_asset("aurora-sans.woff2")
    assert client.get(f"/brand/fonts/aurora-sans.woff2?v={font.version}").content == FONT
    assert "Aurora" in client.get("/accounts/login/").content.decode()


def test_refusals_and_exclusivity(theme_app, subtests):
    from skeleton.appearance.services import activate_theme, create_theme

    admin = platform_admin()
    with subtests.test(case="unknown key"):
        with pytest.raises(ValidationError):
            activate(admin, app_theme="theme_demo:nada")
    with subtests.test(case="two kinds at once"):
        with acting(admin):
            theme = create_theme(actor=admin, data={"name": "Outro"})
            with pytest.raises(ValidationError):
                activate_theme(actor=admin, app_theme=KEY, builtin="vinho", expected_revision=0)
            with pytest.raises(ValidationError):
                activate_theme(actor=admin, app_theme=KEY, theme_id=theme.pk, expected_revision=0)
    with subtests.test(case="switching clears the app key"):
        activate(admin, app_theme=KEY)
        brand = activate(admin, builtin="vinho")
        assert (brand.app_theme, brand.builtin_theme) == ("", "vinho")


def test_an_uninstalled_app_leaves_the_core_theme(theme_app):
    from skeleton.appearance.app_themes import reset_app_themes_cache

    admin = platform_admin()
    activate(admin, app_theme=KEY)
    assert fresh().theme_css
    unregister_theme_app()
    reset_app_themes_cache()
    current = fresh()
    assert current.theme_css == "" and current.logo_source == "" and current.product_name == ""
    client = Client()
    assert client.get("/brand/logo/").status_code == 404 and client.get("/brand/theme.css").content == b""
    assert client.get("/accounts/login/").status_code == 200
    out = StringIO()
    call_command("appearance", "show", stdout=out)
    assert "da app theme_demo (não instalada; vale o tema do núcleo)" in out.getvalue()
    register_theme_app(theme_app)  # Reinstalled: the key still stored brings the theme back.
    reset_app_themes_cache()
    assert fresh().product_name == "Aurora"


def test_files_only_from_the_declared_directory(theme_app, subtests):
    from skeleton.appearance.assets import AssetRef, open_asset
    from skeleton.appearance.snapshot import current_appearance

    admin = platform_admin()
    activate(admin, app_theme=KEY)
    ref = fresh().theme_asset("logo")
    directory = theme_app / "theme_demo" / "themes" / "aurora"
    with subtests.test(case="changed on disk"):
        (directory / "images" / "logo.svg").write_bytes(LOGO_SVG.replace("Aurora", "Outra").encode())
        assert open_asset(ref) is None and Client().get("/brand/logo/").status_code == 404
    with subtests.test(case="undeclared file"):
        (directory / "images" / "extra.png").write_bytes(b"x")
        assert open_asset(AssetRef("logo", "image/png", ref.sha256, f"app:{KEY}:images/extra.png")) is None
    with subtests.test(case="escape"):
        assert open_asset(AssetRef("logo", "image/png", ref.sha256, f"app:{KEY}:../../../manifest.json")) is None
    assert "theme_demo/themes" not in repr(current_appearance())  # The directory never leaves the registry.


def test_a_zip_with_the_id_of_an_app_theme_is_refused(theme_app, settings, tmp_path, subtests):
    from skeleton.appearance.packages import install_theme_package

    settings.STORAGES = {
        **settings.STORAGES,
        "skeleton_files": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "files"), "base_url": None},
        },
    }
    admin = platform_admin()
    for case, identity in (
        ("same id", "org.example.demo.theme.aurora"),
        ("prefix of the app", "org.example.demo.theme.novo"),
    ):
        with subtests.test(case=case):
            data = make_zip(files(manifest_value=manifest(id=identity, compatibility={"skeleton": ">=0.0.0"})))
            with acting(admin), pytest.raises(ValidationError) as error:
                install_theme_package(actor=admin, data=data)
            assert error.value.error_dict["package"][0].code == "id_belongs_to_app"
            assert "Demo de temas" in error.value.messages[0]


def test_list_themes_shows_the_app_themes(theme_app):
    admin = platform_admin()
    activate(admin, app_theme=KEY)
    out = StringIO()
    call_command("appearance", "list-themes", stdout=out)
    assert "app · Aurora · theme_demo 1.2.0 (ativo)" in out.getvalue()
