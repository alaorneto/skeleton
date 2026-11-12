"""The data subject downloads their own data across organizations (#190)."""

import io
import json
import zipfile

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.notifications import registry
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")


@pytest.fixture
def scenario(monkeypatch):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    monkeypatch.setattr(registry, "_types", {})
    monkeypatch.setattr(registry, "_duplicates", [])
    register_notification_type(NotificationType("system.notice", "Aviso"))
    return build_service_scenario(Note, slug="privacy")


def login(user):
    client = Client()
    assert client.post(reverse("account_login"), {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


def exported(client):
    response = client.get(reverse("web:privacy-export"))
    assert response.status_code == 200, response.status_code
    assert response["Content-Disposition"].startswith("attachment;")
    assert "no-store" in response["Cache-Control"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        return json.loads(archive.read("dados.json"))


def test_the_export_covers_every_organization_of_the_person_and_nothing_of_others(scenario):
    with scenario.acting(scenario.admin_a, scenario.a):
        notify("system.notice", recipients=Recipients(users=(scenario.member.pk, scenario.denied.pk)))
    with scenario.acting(scenario.admin_b, scenario.b):
        notify("system.notice", recipients=Recipients(users=(scenario.member.pk,)))
    run_pending_jobs()
    data = exported(login(scenario.member))
    assert data["identity"]["email"] == scenario.member.email
    assert {item["sysid"] for item in data["organizations"]} == {str(scenario.a.pk), str(scenario.b.pk)}
    assert all(len(item["notifications"]) == 1 for item in data["organizations"])
    text = json.dumps(data)
    for other in (scenario.denied, scenario.admin_a, scenario.admin_b, scenario.outsider):
        assert other.email not in text
    assert "digest" not in text  # Token digests never leave.


def test_the_page_needs_a_login_and_shows_the_download(scenario):
    assert Client().get(reverse("web:privacy")).status_code == 302
    page = login(scenario.outsider).get(reverse("web:privacy")).content.decode()
    assert reverse("web:privacy-export") in page
    data = exported(login(scenario.outsider))
    assert data["organizations"] == []  # No membership: only the identity.
