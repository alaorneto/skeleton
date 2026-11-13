"""Attachments authorized by their target record (#59)."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from skeleton.files.sniffing import detect
from skeleton.system import registry
from skeleton.system.registry import AttachmentPolicy
from skeleton.tenancy.context import system_context
from skeleton.testing.registries import table_spec_override
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests import support

pytestmark = pytest.mark.usefixtures("skeleton_db")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
PDF = b"%PDF-1.7\n" + b"x" * 40


def test_sniffing_requires_content_and_extension_to_agree():
    assert detect(PNG, "photo.png") == "image/png"
    assert detect(PNG, "photo.pdf") is None
    assert detect(b"plain text", "photo.png") is None
    assert detect(b"a,b\n1,2\n", "table.csv") == "text/csv"
    assert detect(b"PK\x03\x04rest", "report.docx").endswith("wordprocessingml.document")
    assert detect(b"MZ\x90\x00", "tool.exe") is None
    assert detect(b"<svg onload=alert(1)>", "image.svg") is None


@pytest.fixture
def scenario(monkeypatch):

    from examples.notes.models import Note

    key = "example_notes.note"
    with table_spec_override(key, attachments=AttachmentPolicy(max_bytes=200, types=("image/png", "application/pdf"))):
        yield build_service_scenario(Note, slug="attach")


def login(user):
    return support.login(user, password=FIXTURE_PASSWORD)


def url(scenario, note, organization=None):
    return reverse(
        "api:attachments",
        kwargs={
            "organization_id": (organization or scenario.a).pk,
            "table_key": "example_notes.note",
            "sysid": note.pk,
        },
    )


def upload(client, target, content, name="file.png"):
    return client.post(
        target,
        {"file": SimpleUploadedFile(name, content)},
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )


@pytest.mark.needs_example_app
def test_owner_uploads_lists_and_reader_without_update_gets_403(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="AT-1", title="T", visibility="organization")
    owner = login(scenario.member)
    created = upload(owner, url(scenario, note), PNG)
    assert created.status_code == 201, created.content
    data = created.json()
    assert (data["filename"], data["content_type"], data["state"]) == ("file.png", "image/png", "available")
    assert "storage_key" not in data
    reader = login(scenario.admin_a)  # Admin reads everything; give the reader role by ownership instead.
    other = scenario.create(scenario.a, owner=scenario.admin_a, code="AT-2", title="T2", visibility="organization")
    # member reads other's organization note but is not its owner: list 200, upload 403.
    assert owner.get(url(scenario, other)).status_code == 200
    assert upload(owner, url(scenario, other), PNG).status_code == 403
    assert [row["filename"] for row in reader.get(url(scenario, note)).json()["results"]] == ["file.png"]


@pytest.mark.needs_example_app
def test_foreign_target_fake_type_size_and_no_opt_in(scenario, monkeypatch):
    note = scenario.create(scenario.a, owner=scenario.member, code="AT-3", title="T")
    foreign = scenario.create(scenario.b, owner=scenario.member, code="AT-4", title="T")
    client = login(scenario.member)
    assert upload(client, url(scenario, foreign), PNG).status_code == 404
    assert upload(client, url(scenario, note), b"not really a png", "fake.png").status_code == 400
    assert upload(client, url(scenario, note), PDF, "doc.png").status_code == 400
    assert upload(client, url(scenario, note), PNG + b"x" * 300).status_code == 413
    key = "example_notes.note"
    with table_spec_override(key, attachments=None):
        assert client.get(url(scenario, note)).status_code == 404


@pytest.mark.needs_example_app
def test_deleted_target_hides_attachments_and_delete_restore_need_if_match(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="AT-5", title="T")
    client = login(scenario.member)
    attachment = upload(client, url(scenario, note), PDF, "doc.pdf").json()
    detail = url(scenario, note) + f"{attachment['sysid']}/"
    csrf = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    assert client.delete(detail, **csrf).status_code == 428
    assert client.delete(detail, HTTP_IF_MATCH='"9"', **csrf).status_code == 412
    assert client.delete(detail, HTTP_IF_MATCH='"1"', **csrf).status_code == 204
    assert client.get(url(scenario, note)).json()["results"] == []
    restored = client.post(detail + "restore/", "{}", content_type="application/json", HTTP_IF_MATCH='"2"', **csrf)
    assert restored.status_code == 200 and restored.json()["deleted_at"] is None
    with system_context(reason="test:attach-delete-target", organization_id=scenario.a.pk):
        from examples.notes.models import Note

        Note.objects.get(pk=note.pk).delete()
    assert client.get(url(scenario, note)).status_code == 404


@pytest.mark.needs_example_app
def test_storage_key_is_redacted_in_audit_and_html_panel_uploads(scenario):
    from skeleton.audit.models import AuditEvent

    note = scenario.create(scenario.a, owner=scenario.member, code="AT-6", title="T")
    client = login(scenario.member)
    detail = reverse(
        "web:record", kwargs={"organization_id": scenario.a.pk, "table_key": "example_notes.note", "sysid": note.pk}
    )
    page = client.get(detail).content.decode()
    assert "Anexos" in page and 'enctype="multipart/form-data"' in page
    form_url = reverse(
        "files:upload",
        kwargs={"organization_id": scenario.a.pk, "table_key": "example_notes.note", "sysid": note.pk},
    )
    response = client.post(
        form_url,
        {"file": SimpleUploadedFile("photo.png", PNG), "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302 and "photo.png" in client.get(detail).content.decode()
    with system_context(reason="test:attach-audit", organization_id=scenario.a.pk):
        change = AuditEvent.objects.get(model_label="sk_files.attachment", operation="create").changes["storage_key"]
    assert change == {"old": None, "new": "[REDACTED]", "redacted": True}


@pytest.mark.needs_example_app
def test_storage_object_is_removed_when_the_row_cannot_persist(scenario, monkeypatch):
    from skeleton.files import services, storage
    from skeleton.files.models import Attachment

    note = scenario.create(scenario.a, owner=scenario.member, code="AT-7", title="T")
    keys = []
    original = storage.put_stream

    def recording(key, stream, *, max_bytes):
        keys.append(key)
        return original(key, stream, max_bytes=max_bytes)

    monkeypatch.setattr(services, "put_stream", recording)
    monkeypatch.setattr(Attachment, "save", lambda self, **kwargs: (_ for _ in ()).throw(RuntimeError("db down")))
    with scenario.acting(scenario.member):
        spec = registry.get_spec("example_notes.note")
        with pytest.raises(RuntimeError):
            services.upload(spec, note.pk, user=scenario.member, stream=io.BytesIO(PNG), filename="x.png")
    assert keys and not storage.private_storage().exists(keys[0])


@pytest.mark.needs_example_app
def test_upload_records_real_size_and_digest_on_a_read_based_storage(scenario, monkeypatch):
    """The size, digest and quota come from the bytes read, on a backend that never calls chunks() (#225)."""
    import hashlib

    from skeleton.files import storage
    from skeleton.files.models import Attachment, StorageUsage
    from tests.support import ReadBasedStorage

    backend = ReadBasedStorage()
    monkeypatch.setattr(storage, "private_storage", lambda: backend)
    note = scenario.create(scenario.a, owner=scenario.member, code="AT-S3", title="T")
    created = upload(login(scenario.member), url(scenario, note), PNG)
    assert created.status_code == 201, created.content
    with system_context(reason="test:read-based-size", organization_id=scenario.a.pk):
        attachment = Attachment.objects.get(pk=created.json()["sysid"])
        usage = StorageUsage.objects.get(organization=scenario.a)
    assert attachment.size == len(PNG) and attachment.sha256 == hashlib.sha256(PNG).hexdigest()
    assert usage.bytes_used == len(PNG)
    assert list(backend.objects.values()) == [PNG]


@pytest.mark.needs_example_app
def test_upload_above_policy_limit_answers_413_on_a_read_based_storage(scenario, monkeypatch):
    from skeleton.files import storage
    from tests.support import ReadBasedStorage

    backend = ReadBasedStorage()
    monkeypatch.setattr(storage, "private_storage", lambda: backend)
    note = scenario.create(scenario.a, owner=scenario.member, code="AT-S3-BIG", title="T")
    assert upload(login(scenario.member), url(scenario, note), PNG + b"x" * 300).status_code == 413
    assert backend.objects == {}
