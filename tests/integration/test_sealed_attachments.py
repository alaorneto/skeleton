"""Attachments and comments of a sealed record (#529): frozen by the table's SealPolicy."""

import io
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.service_api import Conflict
from skeleton.service_api.comments import CommentService
from skeleton.service_api.files import FileService
from skeleton.service_api.records import RecordSealService
from skeleton.system.registry import AttachmentPolicy, CommentPolicy, SealPolicy
from skeleton.testing.service_api import build_service_scenario
from tests.integration.sealing_support import TABLE, sealed_document_table

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PDF = b"%PDF-1.4\n" + b"0" * 64
ATTACHMENTS = AttachmentPolicy(max_bytes=1000, types=("application/pdf",))


def build(owner_schema_editor, seal):
    table = sealed_document_table(owner_schema_editor, attachments=ATTACHMENTS, comments=CommentPolicy(), seal=seal)
    model = table.__enter__()
    return table, build_service_scenario(model, slug="sealed-files")


@pytest.fixture
def frozen(owner_schema_editor):
    table, scenario = build(owner_schema_editor, SealPolicy(attachments="frozen", comments="frozen"))
    yield scenario
    table.__exit__(None, None, None)


@pytest.fixture
def open_policy(owner_schema_editor):
    table, scenario = build(owner_schema_editor, SealPolicy(attachments="open", comments="open"))
    yield scenario
    table.__exit__(None, None, None)


def attach(scenario, record, name="doc.pdf"):
    return scenario.caller(
        lambda actor: FileService().attach(
            actor=actor, table_key=TABLE, record_id=record.pk, filename=name, stream=io.BytesIO(PDF)
        )
    )()


def seal(scenario, record):
    return scenario.caller(
        lambda actor: RecordSealService().seal(
            actor=actor, table_key=TABLE, record_id=record.pk, expected_revision=record.revision
        )
    )()


def verify(scenario, record):
    return scenario.caller(
        lambda actor: RecordSealService().verify(actor=actor, table_key=TABLE, record_id=record.pk)
    )()


def conflict(call):
    with pytest.raises(Conflict) as error:
        call()
    return error.value.code


def test_frozen_attachments_refuse_upload_removal_and_restoration_after_the_seal(frozen):
    record = frozen.create(frozen.a, body="x", owner=frozen.member)
    kept, dropped = attach(frozen, record, "kept.pdf"), attach(frozen, record, "dropped.pdf")
    frozen.caller(lambda actor: FileService().remove(actor=actor, attachment_id=dropped.sysid, expected_revision=1))()
    seal(frozen, record)
    assert conflict(lambda: attach(frozen, record)) == "record_sealed"
    assert (
        conflict(
            frozen.caller(
                lambda actor: FileService().remove(actor=actor, attachment_id=kept.sysid, expected_revision=1)
            )
        )
        == "record_sealed"
    )
    client = Client()
    client.force_login(frozen.member)
    kwargs = {"organization_id": frozen.a.pk, "table_key": TABLE, "sysid": record.pk}
    api = client.post(reverse("api:attachments", kwargs=kwargs), {"file": SimpleUploadedFile("x.pdf", PDF)})
    assert api.status_code == 409 and api.json()["code"] == "record_sealed"
    restore = client.post(
        reverse("api:attachment-restore", kwargs={**kwargs, "attachment_id": dropped.sysid}),
        data="",
        content_type="application/json",
        HTTP_IF_MATCH='"2"',
    )
    assert restore.status_code == 409 and restore.json()["code"] == "record_sealed"
    page = client.post(reverse("files:upload", kwargs=kwargs), {"file": SimpleUploadedFile("y.pdf", PDF)})
    assert page.status_code == 302
    download = client.get(reverse("files:download", kwargs={**kwargs, "attachment_id": kept.sysid}))
    assert download.status_code == 200  # Reading stays allowed.
    listed = frozen.caller(lambda actor: FileService().list(actor=actor, table_key=TABLE, record_id=record.pk))()
    assert [item.sysid for item in listed.items] == [kept.sysid]


def test_the_digest_covers_the_attachments_and_verify_detects_a_swap(frozen, owner_schema_editor):
    plain = frozen.create(frozen.a, body="same")
    with_file = frozen.create(frozen.a, body="same")
    attachment = attach(frozen, with_file)
    assert seal(frozen, plain).digest != seal(frozen, with_file).digest
    assert verify(frozen, with_file).valid
    with owner_schema_editor() as editor:
        editor.execute('ALTER TABLE skeleton."sk_files_attachment" DISABLE TRIGGER USER')
        editor.execute(
            'UPDATE skeleton."sk_files_attachment" SET sha256 = %s WHERE sysid = %s', ["f" * 64, attachment.sysid]
        )
        editor.execute('ALTER TABLE skeleton."sk_files_attachment" ENABLE TRIGGER USER')
    assert not verify(frozen, with_file).valid


def test_the_purge_never_reaches_a_live_frozen_attachment(frozen):
    from skeleton.files.models import Attachment
    from skeleton.files.purge import purge_deleted
    from skeleton.tenancy.context import system_context

    record = frozen.create(frozen.a, body="x")
    attachment = attach(frozen, record)
    seal(frozen, record)
    with system_context(reason="test:purge", organization_id=frozen.a.pk):
        purge_deleted(now=timezone.now() + timedelta(days=3650))
    with system_context(reason="test:purge-read", organization_id=frozen.a.pk):
        row = Attachment.objects.get(pk=attachment.sysid)
    assert row.deleted_at is None and row.state != Attachment.State.PURGED


def test_frozen_comments_refuse_new_edited_and_deleted_ones(frozen):
    record = frozen.create(frozen.a, body="x")
    comment = frozen.caller(
        lambda actor: CommentService().add_comment(actor=actor, table_key=TABLE, record_id=record.pk, body="antes")
    )()
    seal(frozen, record)
    add = frozen.caller(
        lambda actor: CommentService().add_comment(actor=actor, table_key=TABLE, record_id=record.pk, body="depois")
    )
    edit = frozen.caller(
        lambda actor: CommentService().edit_comment(
            actor=actor, table_key=TABLE, record_id=record.pk, comment_id=comment.sysid, body="x", expected_revision=1
        )
    )
    delete = frozen.caller(
        lambda actor: CommentService().delete_comment(
            actor=actor, table_key=TABLE, record_id=record.pk, comment_id=comment.sysid, expected_revision=1
        )
    )
    for call in (add, edit, delete):
        assert conflict(call) == "record_sealed"
    page = Client()
    page.force_login(frozen.member)
    response = page.post(
        reverse("comments:add", kwargs={"organization_id": frozen.a.pk, "table_key": TABLE, "sysid": record.pk}),
        {"body": "pela tela"},
    )
    assert response.status_code == 409


def test_an_open_policy_keeps_attachments_and_comments_free_and_outside_the_digest(open_policy):
    record = open_policy.create(open_policy.a, body="x", owner=open_policy.member)
    sealed = seal(open_policy, record)
    attachment = attach(open_policy, record)
    open_policy.caller(
        lambda actor: FileService().remove(actor=actor, attachment_id=attachment.sysid, expected_revision=1)
    )()
    open_policy.caller(
        lambda actor: CommentService().add_comment(actor=actor, table_key=TABLE, record_id=record.pk, body="livre")
    )()
    check = verify(open_policy, record)
    assert check.valid and check.digest == sealed.digest
