"""sync_bundled_apps reports what it mirrored into the deployment state (#335)."""

import io

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.usefixtures("skeleton_db")


def test_sync_bundled_apps_without_first_party_apps(settings):
    settings.SKELETON_BUNDLED_APPS = ()
    out = io.StringIO()
    call_command("sync_bundled_apps", stdout=out)
    assert out.getvalue().splitlines() == ["instaladas: —", "reinstaladas: —", "retidas: —"]
