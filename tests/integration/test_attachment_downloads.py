"""Downloads only through an authorized view, streamed after the transaction (#60)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from skeleton.common.responses import DetachedFileResponse, content_disposition
from skeleton.system.registry import AttachmentPolicy
from skeleton.tenancy.context import system_context
from skeleton.testing.registries import table_spec_override
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests import support

pytestmark = pytest.mark.usefixtures("skeleton_db")
PDF = b"%PDF-1.7\n" + b"y" * 64


def test_headers_force_a_download_that_cannot_execute():
    assert (
        content_disposition('re"port;x.pdf')
        == "attachment; filename=\"re_port_x.pdf\"; filename*=UTF-8''re%22port%3Bx.pdf"
    )
    assert DetachedFileResponse(storage_key="k", filename="a.html", content_type="text/html").file_type == (
        "application/octet-stream"
    )
    assert DetachedFileResponse(storage_key="k", filename="a.svg", content_type="image/svg+xml").file_type == (
        "application/octet-stream"
    )


@pytest.fixture
def scenario(monkeypatch):

    from examples.notes.models import Note

    key = "example_notes.note"
    with table_spec_override(key, attachments=AttachmentPolicy(max_bytes=1000, types=("application/pdf",))):
        yield build_service_scenario(Note, slug="download")


def login(user):
    return support.login(user, password=FIXTURE_PASSWORD)


@pytest.fixture
def shared(scenario):
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="DL-1", title="T", visibility="organization")
    owner = login(scenario.admin_a)
    created = owner.post(
        reverse(
            "api:attachments",
            kwargs={"organization_id": scenario.a.pk, "table_key": "example_notes.note", "sysid": note.pk},
        ),
        {"file": SimpleUploadedFile("relatório.pdf", PDF)},
        HTTP_X_CSRFTOKEN=owner.cookies["csrftoken"].value,
    )
    assert created.status_code == 201, created.content
    url = reverse(
        "files:download",
        kwargs={
            "organization_id": scenario.a.pk,
            "table_key": "example_notes.note",
            "sysid": note.pk,
            "attachment_id": created.json()["sysid"],
        },
    )
    return note, created.json()["sysid"], url


@pytest.mark.needs_example_app
def test_download_streams_with_safe_headers_and_zero_queries(scenario, shared):
    _, _, url = shared
    response = login(scenario.member).get(url)
    assert response.status_code == 200 and response.streaming
    with CaptureQueriesContext(connection) as queries:
        body = b"".join(response.streaming_content)
    assert body == PDF and len(queries.captured_queries) == 0
    assert response["Content-Disposition"].startswith("attachment;")
    assert "filename*=UTF-8''relat%C3%B3rio.pdf" in response["Content-Disposition"]
    assert (response["X-Content-Type-Options"], response["Content-Security-Policy"]) == ("nosniff", "sandbox")
    assert (response["Cache-Control"], response["Referrer-Policy"]) == ("no-store", "no-referrer")


@pytest.mark.needs_example_app
def test_revocation_applies_to_the_next_request(scenario, shared):
    from examples.notes.models import Note

    note, _, url = shared
    client = login(scenario.member)
    assert client.get(url).status_code == 200
    with system_context(reason="test:download-revoke", organization_id=scenario.a.pk):
        locked = Note.objects.get(pk=note.pk)
        locked.visibility = "private"
        locked.save(update_fields={"visibility"})
    assert client.get(url).status_code == 404
    assert login(scenario.denied).get(url).status_code == 404


@pytest.mark.parametrize("state", ["pending", "quarantined", "rejected"])
@pytest.mark.needs_example_app
def test_files_not_available_never_download(scenario, shared, state):
    from skeleton.files.models import Attachment

    _, attachment_id, url = shared
    with system_context(reason="test:download-state", organization_id=scenario.a.pk):
        Attachment.objects.filter(pk=attachment_id).update(state=state)
    assert login(scenario.member).get(url).status_code == 404


@pytest.mark.needs_example_app
def test_accelerated_delivery_hands_the_key_to_the_proxy(scenario, shared):
    _, attachment_id, url = shared
    with override_settings(SKELETON_FILES_ACCEL="nginx", SKELETON_FILES_ACCEL_PREFIX="/_protected/"):
        response = login(scenario.member).get(url)
    assert response.status_code == 200 and not response.streaming
    assert response["X-Accel-Redirect"] == f"/_protected/org/{scenario.a.pk}/{attachment_id}"
    assert response["Content-Disposition"].startswith("attachment;") and response.content == b""
