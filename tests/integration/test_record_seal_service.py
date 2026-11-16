"""RecordSealService, seal_record and the generic surfaces on a sealable table (#527).

A sealable probe table of the example app is declared for each test: the model joins the global
registry under ``example_notes`` (so the catalog, the table permissions and the app activation
apply as to any app table) and is removed afterwards; its table is created by the owner.
"""

import hashlib
from types import SimpleNamespace

import pytest
from django.test import Client

from skeleton.common.sealing import digest
from skeleton.records.sealing import seal_payload, seal_record, verify_seal
from skeleton.service_api import Conflict, Forbidden, InvalidInput
from skeleton.service_api.records import RecordSealService
from skeleton.system import registry
from skeleton.testing.service_api import (
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
    build_service_scenario,
)
from tests.integration.sealing_support import TABLE, sealed_document_table

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
EXTRA = hashlib.sha256(b"child rows").hexdigest()


@pytest.fixture
def document_model(owner_schema_editor):
    with sealed_document_table(owner_schema_editor) as model:
        yield model


@pytest.fixture
def scenario(document_model):
    return build_service_scenario(document_model, slug="record-seal")


def seal(scenario, record, *, revision=None, user=None, organization=None, extra_digest=""):
    return scenario.caller(
        lambda actor: RecordSealService().seal(
            actor=actor,
            table_key=TABLE,
            record_id=record.pk,
            expected_revision=record.revision if revision is None else revision,
            extra_digest=extra_digest,
        ),
        user=user,
        organization=organization,
    )


def test_the_service_honours_the_contract():
    assert_service_contract(RecordSealService)


def test_an_authorized_member_seals_and_verifies(scenario):
    record = scenario.create(scenario.a, body="Assinado")
    info = seal(scenario, record, extra_digest=EXTRA)()
    assert info.sealed and info.revision == 2 and len(info.digest) == 64 and info.sealed_by == scenario.member.pk
    status = scenario.caller(
        lambda actor: RecordSealService().status(actor=actor, table_key=TABLE, record_id=record.pk)
    )()
    assert status == info
    check = scenario.caller(
        lambda actor: RecordSealService().verify(actor=actor, table_key=TABLE, record_id=record.pk, extra_digest=EXTRA)
    )()
    assert check.valid and check.computed == info.digest
    without_extra = scenario.caller(
        lambda actor: RecordSealService().verify(actor=actor, table_key=TABLE, record_id=record.pk)
    )()
    assert not without_extra.valid


def test_a_second_seal_and_a_stale_revision_conflict(scenario):
    first, second = scenario.create(scenario.a, body="x"), scenario.create(scenario.a, body="y")
    seal(scenario, first)()
    with pytest.raises(Conflict) as sealed:
        seal(scenario, first, revision=2)()
    assert sealed.value.code == "record_sealed"
    with pytest.raises(Conflict) as stale:
        seal(scenario, second, revision=5)()
    assert stale.value.code == "stale_revision"


def test_two_seals_with_the_same_revision_have_one_winner(scenario):
    record = scenario.create(scenario.a, body="x")
    seal(scenario, record, revision=1)()
    with pytest.raises(Conflict) as loser:
        seal(scenario, record, revision=1)()
    assert loser.value.code in {"record_sealed", "stale_revision"}


def test_default_deny_private_records_and_other_organizations(scenario):
    record = scenario.create(scenario.a, body="x")
    private = scenario.create(scenario.a, body="x", owner=scenario.admin_a, visibility="private")
    missing = SimpleNamespace(pk="00000000-0000-4000-8000-0000000000ff", revision=1)
    assert_denied_as_not_found(seal(scenario, record, user=scenario.denied), missing=seal(scenario, missing))
    assert_denied_as_not_found(seal(scenario, private), missing=seal(scenario, missing))
    foreign = scenario.create(scenario.b, body="segredo de B")
    assert_tenant_isolated(
        scenario.caller(lambda actor: RecordSealService().status(actor=actor, table_key=TABLE, record_id=foreign.pk)),
        foreign=[foreign],
    )


def test_reading_without_update_is_forbidden(scenario):
    from skeleton.access.models import Action, TablePermission
    from skeleton.tenancy.context import system_context

    record = scenario.create(scenario.a, body="x")
    with system_context(reason="fixture:seal-read-only", organization_id=scenario.a.pk):
        TablePermission.objects.filter(table__model_label=TABLE, action=Action.UPDATE).delete()
    with pytest.raises(Forbidden):
        seal(scenario, record)()


def test_the_app_disabled_and_another_actor_are_refused(scenario):
    record = scenario.create(scenario.a, body="x")
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: RecordSealService().status(actor=actor, table_key=TABLE, record_id=record.pk)),
        disabled=scenario.application_disabled,
    )
    assert_rejects_other_actor(
        scenario.caller(
            lambda actor: RecordSealService().status(actor=scenario.admin_a, table_key=TABLE, record_id=record.pk)
        )
    )


def test_a_table_without_seal_and_a_malformed_extra_are_invalid(scenario):
    record = scenario.create(scenario.a, body="x")
    with pytest.raises(InvalidInput):
        scenario.caller(
            lambda actor: RecordSealService().status(actor=actor, table_key="example_notes.note", record_id=record.pk)
        )()
    with pytest.raises(InvalidInput):
        seal(scenario, record, extra_digest="not-a-digest")()


def test_verify_detects_content_changed_under_the_guard(scenario, owner_schema_editor):
    record = scenario.create(scenario.a, body="original")
    seal(scenario, record)()
    with owner_schema_editor() as editor:
        editor.execute('ALTER TABLE skeleton."sk_test_sealeddocument" DISABLE TRIGGER USER')
        editor.execute('UPDATE skeleton."sk_test_sealeddocument" SET body = %s WHERE sysid = %s', ["forged", record.pk])
        editor.execute('ALTER TABLE skeleton."sk_test_sealeddocument" ENABLE TRIGGER USER')
    check = scenario.caller(
        lambda actor: RecordSealService().verify(actor=actor, table_key=TABLE, record_id=record.pk)
    )()
    assert not check.valid and check.digest != check.computed


def test_seal_record_in_process_and_the_payload(scenario, document_model):
    record = scenario.create(scenario.a, body="Corpo")
    with scenario.as_member() as actor:
        payload = seal_payload(record, extra={"lines": 2})
        assert payload["fields"] == {"body": "Corpo"} and payload["attachments"] == []
        assert payload["table"] == TABLE and payload["organization"] == scenario.a.pk
        sealed = seal_record(record, user=actor, expected_revision=1, extra={"lines": 2})
        assert sealed.seal_digest == digest(seal_payload(sealed, extra={"lines": 2}))
        assert verify_seal(sealed, extra={"lines": 2}) and not verify_seal(sealed)


def api_client(user):
    client = Client()
    client.force_login(user)
    return client


def test_the_api_answers_409_for_frozen_columns_and_200_for_free_ones(scenario):
    record = scenario.create(scenario.a, body="Assinado", owner=scenario.member)
    seal(scenario, record)()
    client = api_client(scenario.member)
    url = f"/api/v1/o/{scenario.a.pk}/tables/{TABLE}/records/{record.pk}/"
    detail = client.get(url)
    assert detail.status_code == 200, detail.content
    body = detail.json()
    assert body["sealed_at"] and body["sealed_by"] == str(scenario.member.pk) and "seal_digest" not in body
    frozen = client.patch(url, {"body": "x"}, content_type="application/json", HTTP_IF_MATCH='"2"')
    assert frozen.status_code == 409 and frozen.json()["code"] == "record_sealed"
    free = client.patch(url, {"status": "sent"}, content_type="application/json", HTTP_IF_MATCH='"2"')
    assert free.status_code == 200, free.content
    deleted = client.delete(url, HTTP_IF_MATCH='"3"')
    assert deleted.status_code == 409 and deleted.json()["code"] == "record_sealed"


def test_the_openapi_schema_shows_the_seal_only_on_sealable_tables(scenario, document_model):
    from skeleton.api.openapi import record_schema

    schema = record_schema(registry.get_spec(TABLE))
    assert {"sealed_at", "sealed_by"} <= set(schema["properties"]) and "seal_digest" not in schema["properties"]
    note = record_schema(registry.get_spec("example_notes.note"))
    assert "sealed_at" not in note["properties"]


def test_a_csv_import_reports_a_sealed_row_without_failing_the_job(scenario):
    from skeleton.transfer.imports import Outcome, _apply_rows

    open_record = scenario.create(scenario.a, body="aberto")
    sealed_record = scenario.create(scenario.a, body="Assinado")
    seal(scenario, sealed_record)()
    outcome = Outcome()
    header = ["sysid", "revision", "title", "body", "status"]
    rows = [
        [str(open_record.pk), "1", "", "editado", "draft"],
        [str(sealed_record.pk), "2", "", "alterado", "draft"],
    ]
    with scenario.as_member() as actor:
        _apply_rows(user=actor, spec=registry.get_spec(TABLE), header=header, rows=iter(rows), outcome=outcome)
    assert not outcome.fatal and outcome.rows == 2
    assert outcome.errors == [(3, "record_sealed", "O registro está selado e não pode mais ser alterado.")]


def test_the_pages_refuse_frozen_columns_and_deletion_of_a_sealed_record(scenario):
    from django.urls import reverse

    record = scenario.create(scenario.a, body="Assinado", owner=scenario.member)
    seal(scenario, record)()
    client = api_client(scenario.member)
    kwargs = {"organization_id": scenario.a.pk, "table_key": TABLE, "sysid": record.pk}
    edit = client.post(
        reverse("web:record-edit", kwargs=kwargs), {"title": "", "body": "x", "status": "draft", "revision": 2}
    )
    assert edit.status_code == 409 and "selado" in edit.content.decode()
    free = client.post(
        reverse("web:record-edit", kwargs=kwargs), {"title": "", "body": "Assinado", "status": "sent", "revision": 2}
    )
    assert free.status_code == 302
    delete = client.post(reverse("web:record-delete", kwargs=kwargs), {"revision": 3})
    assert delete.status_code == 409 and "selado" in delete.content.decode()
