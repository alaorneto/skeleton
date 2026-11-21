"""Theme packages in /platform/appearance/ and on the console (#637): send, update, remove and list."""

import logging
from io import StringIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import Client
from django.urls import reverse

from tests.integration.appearance_support import PASSWORD, login, member, platform_admin
from tests.theme_package_support import files, make_zip, manifest

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_appearance")
INSTALL = "platform:appearance-package-install"


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


def package(version="1.0.0", content=None, **changes):
    return make_zip(files(content, manifest_value=manifest(version=version, **changes)))


def send(client, data, name="aurora.zip"):
    return post(client, reverse(INSTALL), {"package": SimpleUploadedFile(name, data, "application/zip")}, follow=True)


def theme(package_id="org.example.theme.aurora"):
    from skeleton.appearance.models import InstanceTheme

    return InstanceTheme.objects.filter(source="package", package_id=package_id).first()


def test_send_again_and_update(admin_client, caplog):
    admin, client = admin_client
    caplog.set_level(logging.INFO, logger="skeleton.security")
    html = send(client, package()).content.decode()
    assert "Pacote Aurora 1.0.0 instalado. Pré-visualize e ative quando quiser." in html
    installed = theme()
    assert installed is not None
    from skeleton.appearance.services import current_brand

    assert current_brand() is None  # Installing never activates.
    assert "Este pacote já está instalado; nada mudou." in send(client, package()).content.decode()
    html = send(client, package("1.1.0", {"theme.css": b".b{}"})).content.decode()
    assert "Pacote Aurora atualizado de 1.0.0 para 1.1.0." in html
    security = [record for record in caplog.records if record.name == "skeleton.security"]
    names = [getattr(record, "event", "") for record in security]
    assert names.count("appearance.package_installed") == 1 and names.count("appearance.package_updated") == 1
    assert "appearance.package_unchanged" not in names
    logged = repr([record.__dict__ for record in security])
    assert "aurora.zip" not in logged and "platform/branding" not in logged and "Aurora" not in logged


def test_refusals_show_the_message(admin_client, subtests):
    admin, client = admin_client
    with subtests.test(case="css line"):
        response = send(client, package(content={"theme.css": b"a{}\n\n/* <style> */"}))
        assert response.status_code == 400
        assert "Linha 3:" in response.content.decode() and theme() is None
    with subtests.test(case="too large"):
        response = send(client, b"\0" * (4 * 1024 * 1024 + 1))
        assert response.status_code == 400 and "4 MiB" in response.content.decode()
    with subtests.test(case="no file"):
        assert post(client, reverse(INSTALL)).status_code == 400
    with subtests.test(case="unknown field"):
        response = post(client, reverse(INSTALL), {"x": "1", "package": SimpleUploadedFile("a.zip", package())})
        assert response.status_code == 400 and theme() is None
    with subtests.test(case="no csrf"):
        response = Client().post(reverse(INSTALL), {"package": SimpleUploadedFile("a.zip", package())})
        assert response.status_code in (302, 403) and theme() is None


def test_only_the_full_administrator_and_a_recent_authentication(admin_client):
    admin, client = admin_client
    other = login(member().email, PASSWORD)
    assert send(other, package()).status_code == 403 and theme() is None
    session = client.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = post(client, reverse(INSTALL), {"package": SimpleUploadedFile("a.zip", package())})
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    assert theme() is None


def test_catalog_origin_actions_edit_404_and_removal(admin_client):
    admin, client = admin_client
    send(client, package())
    installed = theme()
    html = client.get(reverse("platform:appearance")).content.decode()
    assert "org.example.theme.aurora" in html and "v1.0.0" in html and "Instalar pacote" in html
    assert f'href="{reverse("platform:appearance-theme", args=[installed.pk])}"' not in html  # No "Editar".
    assert f'href="{reverse("platform:appearance-theme-delete", args=[installed.pk])}"' in html
    assert client.get(reverse("platform:appearance-theme", args=[installed.pk])).status_code == 404
    preview = client.get(reverse("platform:appearance-theme-preview", args=[installed.pk])).content.decode()
    assert "Sobre este pacote" in preview and "org.example" in preview
    confirm = client.get(reverse("platform:appearance-theme-delete", args=[installed.pk])).content.decode()
    assert "Remover pacote de tema" in confirm and "org.example.theme.aurora 1.0.0" in confirm
    response = post(client, reverse("platform:appearance-theme-delete", args=[installed.pk]), {"revision": 1})
    assert response.status_code == 302 and theme() is None


def test_the_active_package_cannot_be_removed(admin_client):
    from skeleton.appearance.services import activate_theme
    from tests.integration.appearance_support import acting

    admin, client = admin_client
    send(client, package())
    installed = theme()
    with acting(admin):
        activate_theme(actor=admin, theme_id=installed.pk, expected_revision=0)
    html = client.get(reverse("platform:appearance")).content.decode()
    assert f'href="{reverse("platform:appearance-theme-delete", args=[installed.pk])}"' not in html
    response = post(client, reverse("platform:appearance-theme-delete", args=[installed.pk]), {"revision": 1})
    assert response.status_code == 400 and theme() is not None
    html = send(client, package("1.0.1", {"theme.css": b".z{}"})).content.decode()
    assert "É o tema ativo" in html


def run(*args):
    out = StringIO()
    call_command("appearance", *args, stdout=out)
    return out.getvalue()


def test_console_install_list_and_remove(storage, tmp_path):
    archive = tmp_path / "aurora.zip"
    archive.write_bytes(package())
    assert "instalado (não ativado)" in run("install-package", str(archive))
    assert "nada mudou" in run("install-package", str(archive))
    archive.write_bytes(package("2.0.0", {"theme.css": b".c{}"}))
    assert "atualizado de 1.0.0 para 2.0.0" in run("install-package", str(archive))
    listing = run("list-themes")
    assert (
        "pacote · Aurora · org.example.theme.aurora 2.0.0" in listing
        and "núcleo · Cobalto e Ardósia (ativo)" in listing
    )
    assert "platform/branding" not in listing
    assert "removido" in run("remove-package", "org.example.theme.aurora")
    with pytest.raises(CommandError, match="Nenhum pacote"):
        run("remove-package", "org.example.theme.aurora")
    with pytest.raises(CommandError, match="não existe"):
        run("install-package", str(tmp_path / "nada.zip"))
    archive.write_bytes(package(content={"theme.css": b"@import 'x';"}))
    with pytest.raises(CommandError, match="theme.css"):
        run("install-package", str(archive))


def test_console_refuses_to_remove_the_active_package(storage, tmp_path):
    from skeleton.appearance.services import activate_theme
    from skeleton.tenancy.context import system_context

    archive = tmp_path / "aurora.zip"
    archive.write_bytes(package())
    run("install-package", str(archive))
    with system_context(reason="test:activate"):
        activate_theme(actor=None, theme_id=theme().pk, expected_revision=0)
    with pytest.raises(CommandError, match="tema ativo"):
        run("remove-package", "org.example.theme.aurora")
    assert "(ativo)" in run("list-themes").split("pacote · Aurora")[1]
