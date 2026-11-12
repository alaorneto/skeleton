"""Purge of deleted attachments' bytes after the retention window (#192)."""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.files.purge import purge_deleted
from skeleton.files.storage import private_storage
from skeleton.system.registry import AttachmentPolicy
from skeleton.tenancy.context import system_context
from skeleton.testing.registries import table_spec_override
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PDF = b"%PDF-1.4\n" + b"0" * 64
KEY = "example_notes.note"


@pytest.fixture
def scenario(monkeypatch):

    from examples.notes.models import Note

    with table_spec_override(KEY, attachments=AttachmentPolicy(max_bytes=500, types=("application/pdf",))):
        yield build_service_scenario(Note, slug="purge")


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    client.post(
        reverse("account_login"),
        {"login": user.email, "password": FIXTURE_PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    return client


def attachment(scenario, organization, owner, *, delete=True):
    note = scenario.create(organization, owner=owner, code="P-1", title="T")
    client = login(owner)
    url = reverse("api:attachments", kwargs={"organization_id": organization.pk, "table_key": KEY, "sysid": note.pk})
    csrf = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    data = client.post(url, {"file": SimpleUploadedFile("doc.pdf", PDF)}, **csrf).json()
    detail = url + f"{data['sysid']}/"
    if delete:
        assert client.delete(detail, HTTP_IF_MATCH='"1"', **csrf).status_code == 204
    return client, detail, csrf, data["sysid"]


def row(organization, sysid):
    from skeleton.files.models import Attachment

    with system_context(reason="test:purge-read", organization_id=organization.pk):
        return Attachment.objects.with_deleted().get(pk=sysid)


def purge(organization, *, days):
    with system_context(reason="test:purge", organization_id=organization.pk):
        return purge_deleted(now=timezone.now() + timedelta(days=days))


def test_expired_deleted_attachments_lose_their_bytes_and_cannot_be_restored(scenario):
    client, detail, csrf, sysid = attachment(scenario, scenario.a, scenario.member)
    key = row(scenario.a, sysid).storage_key
    assert purge(scenario.a, days=30) == 0  # Inside the window.
    assert private_storage().exists(key)
    assert purge(scenario.a, days=91) == 1
    assert row(scenario.a, sysid).state == "purged" and not private_storage().exists(key)
    restored = client.post(detail + "restore/", "{}", content_type="application/json", HTTP_IF_MATCH='"3"', **csrf)
    assert restored.status_code == 400
    assert row(scenario.a, sysid).deleted_at is not None
    assert purge(scenario.a, days=200) == 0  # Already purged.


def test_live_attachments_and_other_organizations_are_never_touched(scenario):
    _, _, _, live = attachment(scenario, scenario.a, scenario.member, delete=False)
    _, _, _, foreign = attachment(scenario, scenario.b, scenario.admin_b)
    live_key = row(scenario.a, live).storage_key
    assert purge(scenario.a, days=365) == 0
    assert private_storage().exists(live_key) and row(scenario.a, live).state == "available"
    assert row(scenario.b, foreign).state == "available"  # Purging A never reaches B.


def test_a_restored_attachment_is_skipped(scenario):
    client, detail, csrf, sysid = attachment(scenario, scenario.a, scenario.member)
    restored = client.post(detail + "restore/", "{}", content_type="application/json", HTTP_IF_MATCH='"2"', **csrf)
    assert restored.status_code == 200
    assert purge(scenario.a, days=365) == 0
    assert row(scenario.a, sysid).state == "available"
