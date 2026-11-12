"""CSV import with preview, all-or-nothing application and CAS by sysid and revision (#183)."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from skeleton.transfer.csvio import read_csv

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"


@pytest.fixture
def scenario():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="import")


def login(user):
    client = Client()
    assert client.post(reverse("account_login"), {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


def upload(client, organization, text, name="notas.csv"):
    url = reverse("transfer:import", kwargs={"organization_id": organization.pk, "table_key": TABLE})
    return client.post(url, {"file": SimpleUploadedFile(name, ("﻿" + text).encode("utf-8"))})


def transfer(organization):
    from skeleton.transfer.models import TransferJob

    with system_context(reason="test:import", organization_id=organization.pk):
        return TransferJob.objects.filter(kind="import").order_by("-created_at").first()


def notes(organization):
    from examples.notes.models import Note

    with system_context(reason="test:import-notes", organization_id=organization.pk):
        return {note.code: note for note in Note.objects.all()}


def note_audit_count(organization):
    from skeleton.audit.models import AuditEvent

    with system_context(reason="test:import-audit", organization_id=organization.pk):
        return AuditEvent.objects.filter(table_name="example_notes_note").count()


def apply(client, organization, item):
    url = reverse("transfer:apply", kwargs={"organization_id": organization.pk, "sysid": item.pk})
    return client.post(url)


def report_rows(item):
    from skeleton.files.storage import open_object

    with open_object(item.report_key) as stream:
        header, rows = read_csv(stream.read())
    return [dict(zip(header, row, strict=True)) for row in rows]


def test_preview_writes_nothing_and_apply_creates_every_row(scenario):
    client = login(scenario.member)
    before = note_audit_count(scenario.a)
    assert upload(client, scenario.a, "code;title\nI-1;Primeira\nI-2;'=1+1\n").status_code == 302
    run_pending_jobs()
    item = transfer(scenario.a)
    assert (item.state, item.row_count, item.error_count) == ("previewed", 2, 0)
    assert notes(scenario.a) == {} and note_audit_count(scenario.a) == before  # Nothing, not even audit.
    assert apply(client, scenario.a, item).status_code == 302
    run_pending_jobs()
    assert transfer(scenario.a).state == "applied"
    created = notes(scenario.a)
    assert set(created) == {"I-1", "I-2"} and created["I-2"].title == "=1+1"  # The writer's apostrophe is removed.
    assert all(note.owner_id == scenario.member.pk for note in created.values())


def test_updates_need_the_current_revision_and_errors_block_the_application(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="U-1", title="Antes")
    client = login(scenario.member)
    text = f"sysid;revision;code;title\n{note.pk};{note.revision};U-1;Depois\n{note.pk};99;U-1;Velha\n"
    upload(client, scenario.a, text)
    run_pending_jobs()
    item = transfer(scenario.a)
    assert (item.state, item.error_count) == ("previewed", 1)
    rows = report_rows(item)
    assert rows[0]["linha"] == "3" and rows[0]["coluna"] == "revision"
    assert apply(client, scenario.a, item).status_code == 409  # Only a preview without errors applies.
    assert notes(scenario.a)["U-1"].title == "Antes"


def test_application_is_all_or_nothing(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="A-1", title="Antes")
    client = login(scenario.member)
    upload(client, scenario.a, f"sysid;revision;code;title\n;;A-2;Nova\n{note.pk};{note.revision};A-1;Depois\n")
    run_pending_jobs()
    item = transfer(scenario.a)
    assert (item.state, item.error_count) == ("previewed", 0)
    # Someone changes the record between the preview and the application.
    from examples.notes.models import Note

    with system_context(reason="test:import-race", organization_id=scenario.a.pk):
        current = Note.objects.get(pk=note.pk)
        current.title = "Outra pessoa"
        current.save(update_fields={"title"})
    apply(client, scenario.a, item)
    run_pending_jobs()
    item = transfer(scenario.a)
    assert (item.state, item.error_code, item.error_count) == ("failed", "row_errors", 1)
    assert set(notes(scenario.a)) == {"A-1"}  # The valid new row was rolled back too.
    assert notes(scenario.a)["A-1"].title == "Outra pessoa"


def test_columns_outside_the_write_fields_refuse_the_file(scenario):
    client = login(scenario.member)
    upload(client, scenario.a, f"code;title;owner\nX-1;Um;{scenario.admin_a.pk}\n")
    run_pending_jobs()
    item = transfer(scenario.a)
    assert (item.state, item.error_code) == ("failed", "unknown_columns")
    assert [row["coluna"] for row in report_rows(item)] == ["owner"]
    assert notes(scenario.a) == {}


def test_commas_are_accepted_and_invalid_values_are_reported(scenario):
    client = login(scenario.member)
    upload(client, scenario.a, "code,title\nC-1,Um\n,Sem código\n")
    run_pending_jobs()
    item = transfer(scenario.a)
    assert (item.state, item.row_count, item.error_count) == ("previewed", 2, 1)
    assert report_rows(item)[0]["coluna"] == "code"


@override_settings(SKELETON_IMPORT_MAX_ROWS=1)
def test_above_the_row_limit_the_import_fails(scenario):
    client = login(scenario.member)
    upload(client, scenario.a, "code;title\nL-1;Um\nL-2;Dois\n")
    run_pending_jobs()
    assert (transfer(scenario.a).state, transfer(scenario.a).error_code) == ("failed", "too_many_rows")


def test_only_people_who_create_records_import_and_only_the_requester_sees_the_report(scenario):
    url = reverse("transfer:import", kwargs={"organization_id": scenario.a.pk, "table_key": TABLE})
    assert login(scenario.denied).get(url).status_code == 404
    assert login(scenario.admin_b).get(url).status_code == 404
    client = login(scenario.member)
    assert client.get(url).status_code == 200
    upload(client, scenario.a, "code;title\n;Sem código\n")
    run_pending_jobs()
    item = transfer(scenario.a)
    report = reverse("transfer:report", kwargs={"organization_id": scenario.a.pk, "sysid": item.pk})
    assert client.get(report).status_code == 200
    assert login(scenario.admin_a).get(report).status_code == 404
    assert apply(login(scenario.admin_a), scenario.a, item).status_code == 404


def test_non_utf8_uploads_are_refused(scenario):
    client = login(scenario.member)
    url = reverse("transfer:import", kwargs={"organization_id": scenario.a.pk, "table_key": TABLE})
    response = client.post(url, {"file": SimpleUploadedFile("x.csv", "código;título\n".encode("latin-1"))})
    assert response.status_code == 400 and transfer(scenario.a) is None
