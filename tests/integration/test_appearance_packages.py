"""Installing, updating and removing theme packages (#635): CAS, versions, storage, orphans and audit."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, connections
from django.utils import timezone

from skeleton.common.exceptions import ConcurrentUpdate
from tests.integration.appearance_support import acting, audit_events, member, platform_admin
from tests.theme_package_support import files, make_zip, manifest

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
    from skeleton.files.storage import private_storage

    return private_storage()


def stored():
    from skeleton.files.storage import list_prefix

    return set(list_prefix("platform/branding/"))


def package(version="1.0.0", **changes):
    content = files(changes.pop("files", None), manifest_value=manifest(version=version, **changes))
    return make_zip(content)


def install(user, data):
    from skeleton.appearance.packages import install_theme_package

    with acting(user):
        return install_theme_package(actor=user, data=data)


def code_of(error):
    return error.value.error_dict["package"][0].code


def test_install_again_and_update(storage):
    from skeleton.appearance.packages import stored_files

    admin = platform_admin()
    first = install(admin, package())
    theme = first.theme
    assert first.outcome == "installed" and theme.source == "package"
    assert (theme.package_id, theme.package_version, theme.name) == ("org.example.theme.aurora", "1.0.0", "Aurora")
    assert (theme.product_name, theme.font_family, theme.radius) == ("Aurora", "Aurora Sans", "round")
    assert set(theme.package_files) == {"logo", "logo_dark", "icon", "fonts"}
    assert set(stored_files(theme)) == stored() and len(stored()) == 4
    again = install(admin, package())
    assert again.outcome == "unchanged" and again.theme.revision == theme.revision
    newer = install(admin, package("1.1.0", files={"theme.css": b".b{color:red}"}))
    assert (newer.outcome, newer.previous_version) == ("updated", "1.0.0")
    assert newer.theme.pk == theme.pk and newer.theme.revision == theme.revision + 1
    assert newer.theme.custom_css == ".b{color:red}" and len(stored()) == 8  # Old files wait for the job.


def test_versions_and_refusals_write_nothing(storage, subtests):
    admin = platform_admin()
    install(admin, package("1.1.0"))
    count = len(stored())
    cases = [
        ("same version, other content", package("1.1.0", files={"theme.css": b".c{}"}), "same_version_changed"),
        ("downgrade", package("1.0.0", files={"theme.css": b".c{}"}), "downgrade"),
        ("not a zip", b"texto", "archive"),
        ("bad css", package("2.0.0", files={"theme.css": b"a{}\n/* <style> */"}), "css"),
        ("contrast", package("2.0.0", tokens={"brand_color": "#ffff00"}), "brand_color"),
        ("core range", package("2.0.0", compatibility={"skeleton": "<0.0.1"}), "incompatible_core"),
        ("name taken", package(id="org.example.theme.outro"), "name_taken"),
    ]
    for case, data, code in cases:
        with subtests.test(case=case):
            with pytest.raises(ValidationError) as error:
                install(admin, data)
            assert code_of(error) == code
            assert len(stored()) == count


def test_only_the_platform_administrator_installs(storage):
    user = member()
    with pytest.raises(PermissionDenied):
        install(user, package())
    assert stored() == set()


def test_the_51st_theme_is_refused(storage, monkeypatch):
    from skeleton.appearance import services

    monkeypatch.setattr(services, "MAX_THEMES", 1)
    admin = platform_admin()
    with acting(admin):
        services.create_theme(actor=admin, data={"name": "Único"})
    with pytest.raises(ValidationError) as error:
        install(admin, package())
    assert code_of(error) == "too_many_themes"


def test_form_edit_refused_delete_and_activate(storage):
    from skeleton.appearance import services
    from skeleton.appearance.snapshot import current_appearance

    admin = platform_admin()
    theme = install(admin, package()).theme
    with acting(admin):
        with pytest.raises(ValidationError) as error:
            services.update_theme(actor=admin, theme_id=theme.pk, data={"name": "X"}, expected_revision=1)
        assert error.value.code == "package_read_only"
        services.activate_theme(actor=admin, theme_id=theme.pk, expected_revision=0)
        current = current_appearance()
        assert current.brand_color == "#1d4ed8" and "--acme-accent" in current.theme_css
        assert "--radius-base:.75rem" in current.theme_css_tokens
        with pytest.raises(ValidationError):
            services.delete_theme(actor=admin, theme_id=theme.pk, expected_revision=1)
    updated = install(admin, package("1.0.1", files={"theme.css": b".d{}"}))
    assert updated.outcome == "updated" and ".d{}" in current_appearance().theme_css  # Still active.
    with acting(admin):
        services.activate_theme(actor=admin, expected_revision=services.current_brand().revision)
        services.delete_theme(actor=admin, theme_id=theme.pk, expected_revision=updated.theme.revision)
        assert services.list_themes() == []
    assert install(admin, package()).outcome == "installed"  # A removed package can come back.


def test_removed_files_go_with_the_orphans_job(storage):
    from skeleton.appearance import services
    from skeleton.appearance.jobs import clean_orphans
    from skeleton.appearance.packages import stored_files
    from skeleton.tenancy.context import system_context

    admin = platform_admin()
    first = install(admin, package()).theme
    old_keys = set(stored_files(first))
    second = install(admin, package("2.0.0")).theme
    live = set(stored_files(second))
    other = install(admin, package(id="org.example.theme.dois", name="Dois")).theme
    with acting(admin):
        services.delete_theme(actor=admin, theme_id=other.pk, expected_revision=other.revision)
    removed = set(stored_files(other))
    for key in stored():
        old = (timezone.now() - timedelta(hours=2)).timestamp()
        os.utime(storage.path(key), (old, old))
    with system_context(reason="test:appearance-orphans"):
        assert clean_orphans() == len(old_keys) + len(removed)
    assert stored() == live


def test_audit_redacts_the_storage_keys(storage):
    admin = platform_admin()
    theme = install(admin, package()).theme
    events = audit_events(admin, theme.pk)
    assert events
    text = str(events[0].changes)
    assert "org.example.theme.aurora" in text and "--acme-accent" in text
    assert "platform/branding/" not in text and "[REDACTED]" in text


def test_two_installs_of_a_new_version_have_one_winner(storage):
    connection.ensure_connection()
    admin = platform_admin()
    install(admin, package())
    newer = package("2.0.0")
    barrier = Barrier(2)

    def run(_):
        connections.close_all()
        try:
            barrier.wait(10)
            try:
                return install(admin, newer).outcome
            except ConcurrentUpdate:
                return "lost"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2), timeout=60)) == ["unchanged", "updated"]

    from skeleton.appearance.models import InstanceTheme

    with acting(admin):
        assert InstanceTheme.objects.filter(source="package").count() == 1
