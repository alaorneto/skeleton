"""FileService of the core service API (#109)."""

import io
from dataclasses import replace

import pytest
from django.test import override_settings

from skeleton.service_api.errors import Conflict, Forbidden, InvalidInput, NotFound
from skeleton.service_api.files import FileService
from skeleton.system import registry
from skeleton.system.registry import AttachmentPolicy
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"
PDF = b"%PDF-1.7\n" + b"z" * 64


def test_contract():
    assert_service_contract(FileService)


@pytest.fixture
def scenario(monkeypatch):

    from examples.notes.models import Note

    monkeypatch.setitem(
        registry._registry,
        TABLE,
        replace(registry._registry[TABLE], attachments=AttachmentPolicy(max_bytes=1000, types=("application/pdf",))),
    )
    return build_service_scenario(Note, slug="file-service")


def attach(service, actor, note, *, content=PDF, name="doc.pdf"):
    return service.attach(actor=actor, table_key=TABLE, record_id=note.pk, filename=name, stream=io.BytesIO(content))


@pytest.mark.needs_example_app
def test_attach_list_download_url_and_remove_with_cas(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="FS-1", title="T")
    service = FileService()
    with acting_as(scenario.member, scenario.a) as actor:
        info = attach(service, actor, note)
        assert (info.filename, info.content_type, info.state, info.record_id) == (
            "doc.pdf",
            "application/pdf",
            "available",
            note.pk,
        )
        assert [item.sysid for item in service.list(actor=actor, table_key=TABLE, record_id=note.pk).items] == [
            info.sysid
        ]
        url = service.download_url(actor=actor, attachment_id=info.sysid)
        assert url.endswith(f"/attachments/{info.sysid}/download/")
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(Conflict) as stale:
            service.remove(actor=actor, attachment_id=info.sysid, expected_revision=7)
        assert stale.value.code == "stale_revision"
        service.remove(actor=actor, attachment_id=info.sysid, expected_revision=1)
    with acting_as(scenario.member, scenario.a) as actor:
        assert service.list(actor=actor, table_key=TABLE, record_id=note.pk).items == ()


@pytest.mark.needs_example_app
def test_reader_without_update_is_forbidden_and_foreign_target_not_found(scenario):
    shared = scenario.create(scenario.a, owner=scenario.admin_a, code="FS-2", title="T", visibility="organization")
    foreign = scenario.create(scenario.b, owner=scenario.member, code="FS-3", title="B secret")
    own = scenario.create(scenario.a, owner=scenario.member, code="FS-4", title="T")
    service = FileService()
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(Forbidden):
            attach(service, actor, shared)
        with pytest.raises(NotFound):
            attach(service, actor, foreign)
        with pytest.raises(NotFound):
            service.list(actor=actor, table_key="sk_accounts.user", record_id=actor.pk)
        with pytest.raises(InvalidInput):
            attach(service, actor, own, content=b"text", name="fake.pdf")
    with acting_as(scenario.member, scenario.b) as actor_b:
        foreign_file = attach(service, actor_b, foreign)
    assert_tenant_isolated(
        scenario.caller(lambda actor: service.list(actor=actor, table_key=TABLE, record_id=shared.pk)),
        foreign=[foreign, foreign_file.sysid],
    )
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(NotFound):
        service.download_url(actor=actor, attachment_id=foreign_file.sysid)
    assert_rejects_other_actor(
        scenario.caller(lambda actor: service.list(actor=scenario.denied, table_key=TABLE, record_id=shared.pk))
    )


@pytest.mark.needs_example_app
def test_quota_exceeded_is_a_conflict(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="FS-5", title="T")
    service = FileService()
    with (
        override_settings(SKELETON_FILES_ORG_QUOTA_BYTES=len(PDF) + 10),
        acting_as(scenario.member, scenario.a) as actor,
    ):
        attach(service, actor, note)
        with pytest.raises(Conflict) as error:
            attach(service, actor, note, name="second.pdf")
    assert error.value.code == "quota_exceeded"


@pytest.mark.needs_example_app
def test_pending_or_quarantined_has_no_download_url(scenario):
    from skeleton.files.models import Attachment
    from skeleton.tenancy.context import system_context

    note = scenario.create(scenario.a, owner=scenario.member, code="FS-6", title="T")
    service = FileService()
    with acting_as(scenario.member, scenario.a) as actor:
        info = attach(service, actor, note)
    with system_context(reason="test:file-service-quarantine", organization_id=scenario.a.pk):
        Attachment.objects.filter(pk=info.sysid).update(state="quarantined")
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(NotFound):
        service.download_url(actor=actor, attachment_id=info.sysid)
