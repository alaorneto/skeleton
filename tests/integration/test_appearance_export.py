"""Export a theme of the catalog as a theme package (#638): the zip the installation accepts back."""

import zipfile
from io import BytesIO, StringIO

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.urls import reverse

from skeleton.appearance.package_format import read_theme_archive
from tests.integration.appearance_support import PASSWORD, acting, login, member, platform_admin, png, upload
from tests.theme_package_support import files, make_zip

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


def export(user, **kwargs):
    from skeleton.appearance.export import export_theme_package

    with acting(user):
        return export_theme_package(actor=user, **kwargs)


def test_a_form_theme_goes_and_comes_back_the_same(storage):
    from skeleton.appearance import services
    from skeleton.appearance.packages import install_theme_package
    from skeleton.appearance.themes import theme_css

    admin = platform_admin()
    data = {"name": "Mar Aberto", "brand_color": "#0f766e", "radius": "round", "font": "serif", "custom_css": ".a{}"}
    with acting(admin):
        theme = services.create_theme(actor=admin, data=data)
    name, archive = export(admin, theme_id=theme.pk)
    package = read_theme_archive(archive)
    assert name == "local.theme.mar-aberto-1.0.0.zip" and package.id == "local.theme.mar-aberto"
    before = theme_css(services.active_theme_values() or _values(theme))
    with acting(admin):
        services.delete_theme(actor=admin, theme_id=theme.pk, expected_revision=theme.revision)
        installed = install_theme_package(actor=admin, data=archive).theme
    assert (installed.brand_color, installed.radius, installed.font, installed.custom_css) == (
        "#0f766e",
        "round",
        "serif",
        ".a{}",
    )
    assert theme_css(_values(installed)) == before


def _values(theme):
    from skeleton.appearance.themes import ThemeValues

    return ThemeValues(theme.name, theme.brand_color, theme.radius, theme.font, theme.custom_css)


def test_a_package_theme_keeps_its_digest_and_builtins_export(storage):
    from skeleton.appearance.packages import install_theme_package

    admin = platform_admin()
    original = make_zip(files())
    with acting(admin):
        theme = install_theme_package(actor=admin, data=original).theme
    _name, archive = export(admin, theme_id=theme.pk)
    assert read_theme_archive(archive).content_digest == theme.package_digest
    name, builtin = export(admin, builtin="esmeralda")
    package = read_theme_archive(builtin)
    assert name == "local.theme.esmeralda-1.0.0.zip" and package.brand_color == "#047857"


def test_the_brand_of_the_instance_goes_with_include_brand(storage):
    from skeleton.appearance.images import replace_image
    from skeleton.appearance.packages import install_theme_package
    from skeleton.appearance.services import update_brand

    admin = platform_admin()
    with acting(admin):
        theme = install_theme_package(actor=admin, data=make_zip(files())).theme
        update_brand(actor=admin, data={"product_name": "Acme", "footer_text": ""}, expected_revision=0)
        replace_image(actor=admin, kind="logo", upload=upload(png(64, 32), "l.png"), expected_revision=1)
    _name, archive = export(admin, theme_id=theme.pk, include_brand=True)
    package = read_theme_archive(archive)
    assert package.product_name == "Acme" and package.footer_text == "Aurora · atendimento ao cliente"
    assert package.image("logo").mime == "image/png" and package.image("logo_dark") is None  # Same origin.
    with zipfile.ZipFile(BytesIO(archive)) as zipped:
        assert "images/logo.png" in zipped.namelist() and "images/logo.svg" not in zipped.namelist()


def test_refusals(storage, subtests):
    from skeleton.appearance import services

    admin = platform_admin()
    with subtests.test(case="only the administrator"):
        with pytest.raises(PermissionDenied):
            export(member(), builtin="vinho")
    with subtests.test(case="unknown"):
        with pytest.raises(ValidationError):
            export(admin, builtin="nada")
    with subtests.test(case="an old css the reader refuses today"):
        with acting(admin):
            theme = services.create_theme(actor=admin, data={"name": "Velho"})
            theme.custom_css = "a{}\n/* < */"  # Bypasses the service on purpose: an old row.
            theme.save(update_fields={"custom_css"})
        with pytest.raises(ValidationError) as error:
            export(admin, theme_id=theme.pk)
        assert "Linha 2" in " ".join(error.value.messages)


def test_download_only_by_post_of_the_administrator(storage):
    admin = platform_admin()
    client = login(admin.email)
    url = reverse("platform:appearance-builtin-export", args=["vinho"])
    page = client.get(url)
    assert page.status_code == 200 and "Baixar pacote" in page.content.decode()
    response = client.post(url, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert response.status_code == 200 and response["Content-Type"] == "application/zip"
    assert response["Content-Disposition"] == 'attachment; filename="local.theme.vinho-1.0.0.zip"'
    assert "no-store" in response["Cache-Control"]
    assert read_theme_archive(response.content).brand_color == "#9f1239"
    html = client.get(reverse("platform:appearance")).content.decode()
    assert url in html
    other = login(member().email, PASSWORD)
    assert other.get(url).status_code == 403


def test_console_export(storage, tmp_path):
    target = tmp_path / "out" / "grafite.zip"
    out = StringIO()
    call_command("appearance", "export-theme", "--builtin", "grafite", "--output", str(target), stdout=out)
    assert (
        "Id local local.theme.grafite" in out.getvalue() and read_theme_archive(target.read_bytes()).name == "Grafite"
    )
    out = StringIO()
    call_command("appearance", "check-package", str(target), stdout=out)
    assert "Pacote válido." in out.getvalue()
