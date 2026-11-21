"""Logo, dark logo and icon of the instance (#596): CAS, checks by content, cleanup and the job of orphans."""

import io
import os
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from skeleton.common.exceptions import ConcurrentUpdate
from tests.integration.appearance_support import acting, audit_events, platform_admin, png, upload

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_appearance")
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
GIF = b"GIF89a" + b"\x00" * 64


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


def stored(storage):
    from skeleton.files.storage import list_prefix

    return set(list_prefix("platform/branding/"))


def age(storage, key, hours=2):
    old = (timezone.now() - timedelta(hours=hours)).timestamp()
    os.utime(storage.path(key), (old, old))


def test_upload_replace_and_remove_each_image(storage, subtests):
    from skeleton.appearance.images import open_image, remove_image, replace_image
    from skeleton.appearance.services import current_brand, revision_of
    from skeleton.appearance.snapshot import current_appearance

    admin = platform_admin()
    contents = {"logo": (png(64, 32), "logo.png"), "logo_dark": (WEBP, "logo.webp"), "icon": (png(), "icon.png")}
    for kind, (content, name) in contents.items():
        with subtests.test(kind=kind):
            with acting(admin):
                first = replace_image(
                    actor=admin, kind=kind, upload=upload(content, name), expected_revision=revision_of(current_brand())
                )
            key = getattr(first, f"{kind}_key")
            assert key.startswith("platform/branding/") and stored(storage) >= {key}
            with acting(admin):
                assert open_image(kind)[0] == content
                assert current_appearance().__getattribute__(f"{kind}_version") == key[-12:]
                second = replace_image(
                    actor=admin, kind=kind, upload=upload(content, name), expected_revision=first.revision
                )
            assert key not in stored(storage)  # The replaced object goes after the commit.
            with acting(admin):
                removed = remove_image(actor=admin, kind=kind, expected_revision=second.revision)
                assert getattr(removed, f"{kind}_key") == "" and open_image(kind) is None
            assert stored(storage) == set()
    with acting(admin):
        brand_id = current_brand().pk
    events = audit_events(admin, brand_id)
    text = str([event.changes for event in events])
    assert "platform/branding" not in text  # The keys are redacted in the audit.


def test_refusals_by_content(storage, subtests):
    from skeleton.appearance.images import replace_image

    admin = platform_admin()
    cases = [
        ("logo", SVG, "logo.svg"),
        ("logo", GIF, "logo.gif"),
        ("logo", b"\x89PNG\r\n\x1a\n" + b"\x00" * (512 * 1024 + 1), "logo.png"),
        ("logo_dark", SVG, "logo.png"),  # Forged by the name: the content decides.
        ("icon", png(256, 256), "icon.png"),
        ("icon", WEBP, "icon.png"),
        ("icon", png(64, 32)[:30] + b"\x00" * (256 * 1024), "icon.png"),
    ]
    with acting(admin):
        for kind, content, name in cases:
            with subtests.test(kind=kind, name=name, size=len(content)):
                with pytest.raises(ValidationError) as error:
                    replace_image(actor=admin, kind=kind, upload=upload(content, name), expected_revision=0)
                assert kind in error.value.message_dict
        with subtests.test(case="unknown kind"), pytest.raises(ValidationError):
            replace_image(actor=admin, kind="banner", upload=upload(png()), expected_revision=0)
    assert stored(storage) == set()


def test_wrong_revision_is_refused_before_any_byte(storage):
    from skeleton.appearance.images import remove_image, replace_image

    admin = platform_admin()
    with acting(admin):
        with pytest.raises(ConcurrentUpdate):
            replace_image(actor=admin, kind="logo", upload=upload(png(64, 32)), expected_revision=3)
        with pytest.raises(ConcurrentUpdate):
            remove_image(actor=admin, kind="logo", expected_revision="0")
    assert stored(storage) == set()


def test_a_rollback_leaves_an_orphan_that_the_job_removes(storage):
    from skeleton.appearance.images import replace_image
    from skeleton.appearance.jobs import clean_orphans
    from skeleton.files.storage import put_stream
    from skeleton.tenancy.context import system_context

    admin = platform_admin()
    with acting(admin):
        current = replace_image(actor=admin, kind="icon", upload=upload(png()), expected_revision=0)
        with pytest.raises(RuntimeError), transaction.atomic():
            replace_image(actor=admin, kind="logo", upload=upload(png(64, 32)), expected_revision=1)
            raise RuntimeError("rollback")
    orphans = stored(storage) - {current.icon_key}
    assert len(orphans) == 1
    # An object of an organization is never listed nor removed by this job.
    org_key = "org/00000000-0000-4000-8000-000000000001/branding/00000000-0000-4000-8000-000000000002"
    put_stream(org_key, io.BytesIO(b"x"), max_bytes=10)
    with system_context(reason="test:appearance-orphans"):
        assert clean_orphans() == 0  # Inside the grace period.
    for key in (*orphans, current.icon_key, org_key):
        age(storage, key)
    with system_context(reason="test:appearance-orphans"):
        assert clean_orphans() == 1
    assert stored(storage) == {current.icon_key}  # The current images are never removed.
    assert storage.exists(org_key)


def test_open_image_refuses_a_key_outside_the_prefix_and_the_site_never_serves_it(storage):
    from skeleton.appearance.images import open_image, replace_image
    from skeleton.tenancy.branding import LOGO_TYPES, open_site_image

    admin = platform_admin()
    with acting(admin):
        record = replace_image(actor=admin, kind="logo", upload=upload(png(64, 32)), expected_revision=0)
        # An organization route never serves a platform key.
        assert open_site_image(record.logo_key, record.pk, LOGO_TYPES, ("logo.png",)) is None
        record.logo_key = "org/00000000-0000-4000-8000-000000000001/branding/00000000-0000-4000-8000-000000000002"
        record.save(update_fields={"logo_key"})
        assert open_image("logo") is None
        assert open_image("banner") is None


def test_files_orphans_never_touches_the_platform_prefix(storage):
    from skeleton.files.orphans import orphan_keys
    from skeleton.tenancy.context import system_context

    admin = platform_admin()
    from skeleton.appearance.images import replace_image

    with acting(admin):
        record = replace_image(actor=admin, kind="logo", upload=upload(png(64, 32)), expected_revision=0)
    age(storage, record.logo_key)
    organization_id = "00000000-0000-4000-8000-000000000009"
    with system_context(reason="test:files-orphans", organization_id=organization_id):
        assert orphan_keys(organization_id) == []
    assert stored(storage) == {record.logo_key}
