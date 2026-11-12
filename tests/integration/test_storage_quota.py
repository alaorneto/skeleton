"""Storage quota per organization: reservation, release, restore, recount and platform (#61).

Runs on PostgreSQL with the runtime role.
"""

from dataclasses import replace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from skeleton.files.models import StorageQuota, StorageUsage
from skeleton.files.quota import recount, usage_for
from skeleton.system import registry
from skeleton.system.registry import AttachmentPolicy
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 92  # 100 bytes


@pytest.fixture
def scenario(monkeypatch, settings):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    key = "example_notes.note"
    monkeypatch.setitem(
        registry._registry,
        key,
        replace(registry._registry[key], attachments=AttachmentPolicy(max_bytes=1000, types=("image/png",))),
    )
    settings.SKELETON_FILES_ORG_QUOTA_BYTES = 250
    return build_service_scenario(Note, slug="quota")


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": FIXTURE_PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def url(organization, note):
    return reverse(
        "api:attachments",
        kwargs={"organization_id": organization.pk, "table_key": "example_notes.note", "sysid": note.pk},
    )


def upload(client, target, name="file.png"):
    return client.post(
        target, {"file": SimpleUploadedFile(name, PNG)}, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value
    )


def used(organization):
    with system_context(reason="test:quota-usage", organization_id=organization.pk):
        return usage_for(organization.pk).bytes_used


def test_reservation_release_and_restore_above_the_quota(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="Q-1", title="Q")
    client = login(scenario.member)
    first = upload(client, url(scenario.a, note)).json()
    upload(client, url(scenario.a, note))
    assert used(scenario.a) == 200
    refused = upload(client, url(scenario.a, note))
    assert (refused.status_code, refused.json()["code"]) == (409, "quota_exceeded")
    assert used(scenario.a) == 200  # A refused upload never counts.
    csrf = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    detail = url(scenario.a, note) + f"{first['sysid']}/"
    assert client.delete(detail, HTTP_IF_MATCH='"1"', **csrf).status_code == 204
    assert used(scenario.a) == 100
    assert upload(client, url(scenario.a, note)).status_code == 201
    restore = client.post(detail + "restore/", "{}", content_type="application/json", HTTP_IF_MATCH='"2"', **csrf)
    assert (restore.status_code, restore.json()["code"]) == (409, "quota_exceeded")
    assert used(scenario.a) == 200
    # Another organization has its own counter.
    other = scenario.create(scenario.b, owner=scenario.member, code="Q-2", title="B")
    assert upload(client, url(scenario.b, other)).status_code == 201
    assert used(scenario.b) == 100


def test_html_upload_above_the_quota_is_a_409_page(scenario, settings):
    settings.SKELETON_FILES_ORG_QUOTA_BYTES = 50
    note = scenario.create(scenario.a, owner=scenario.member, code="Q-3", title="Q")
    client = login(scenario.member)
    target = reverse(
        "files:upload", kwargs={"organization_id": scenario.a.pk, "table_key": "example_notes.note", "sysid": note.pk}
    )
    response = client.post(
        target,
        {"file": SimpleUploadedFile("file.png", PNG), "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 409 and "cota" in response.content.decode()


def test_recount_corrects_a_planted_divergence(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="Q-4", title="Q")
    upload(login(scenario.member), url(scenario.a, note))
    with system_context(reason="test:quota-plant", organization_id=scenario.a.pk):
        StorageUsage.objects.filter(organization_id=scenario.a.pk).update(bytes_used=999)
    assert used(scenario.a) == 999
    with system_context(reason="test:quota-recount", organization_id=scenario.a.pk):
        assert recount() == 100
        assert StorageUsage.objects.get(organization_id=scenario.a.pk).recounted_at is not None
    assert used(scenario.a) == 100


def test_platform_override_wins_over_the_default(scenario):
    from skeleton.accounts.services import bootstrap_identity

    platform, _ = bootstrap_identity(
        email="root-quota@example.test", organization="quota-platform", name="Quota platform", password=FIXTURE_PASSWORD
    )
    note = scenario.create(scenario.a, owner=scenario.member, code="Q-5", title="Q")
    page = reverse("platform:organization", kwargs={"sysid": scenario.a.pk})
    storage = reverse("platform:organization-storage", kwargs={"sysid": scenario.a.pk})
    member = login(scenario.member)
    assert member.get(page).status_code == 403
    assert (
        member.post(
            storage, {"mode": "unlimited", "csrfmiddlewaretoken": member.cookies["csrftoken"].value}
        ).status_code
        == 403
    )
    admin = login(platform)
    html = admin.get(page).content.decode()
    assert "Armazenamento" in html and "padrão da instância" in html
    csrf = admin.cookies["csrftoken"].value
    assert admin.post(storage, {"mode": "limited", "csrfmiddlewaretoken": csrf}).status_code == 400
    assert admin.post(storage, {"mode": "limited", "quota_mib": "1", "csrfmiddlewaretoken": csrf}).status_code == 302
    row = StorageQuota.objects.get(quota_organization=scenario.a)
    assert row.quota_bytes == 1024 * 1024
    for _ in range(3):
        assert upload(member, url(scenario.a, note)).status_code == 201  # 300 bytes > default 250.
    stale = admin.post(storage, {"mode": "unlimited", "csrfmiddlewaretoken": csrf})
    assert stale.status_code == 409  # The form had no revision of the existing row.
    fresh = admin.post(
        storage, {"mode": "default", "expected_revision": str(row.revision), "csrfmiddlewaretoken": csrf}
    )
    assert fresh.status_code == 302 and not StorageQuota.objects.filter(quota_organization=scenario.a).exists()
    assert upload(member, url(scenario.a, note)).status_code == 409
