"""CSV export of authorized records by job (#182)."""

from datetime import timedelta

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from skeleton.transfer.csvio import BOM, cell, read_csv, write_csv

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    return build_service_scenario(Note, slug="export")


def login(user):
    client = Client()
    assert client.post(reverse("account_login"), {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


def export_url(organization):
    return reverse("transfer:export", kwargs={"organization_id": organization.pk, "table_key": TABLE})


def transfers(organization, **filters):
    from skeleton.transfer.models import TransferJob

    with system_context(reason="test:export", organization_id=organization.pk):
        return list(TransferJob.objects.filter(**filters).order_by("created_at"))


def csv_content(transfer):
    from skeleton.files.storage import open_object

    with open_object(transfer.file_key) as stream:
        return stream.read()


def test_cells_are_formatted_for_excel_and_formulas_are_neutralized():
    from datetime import datetime
    from decimal import Decimal

    assert cell("=SUM(A1:A9)") == "'=SUM(A1:A9)"
    assert cell("+55 11") == "'+55 11" and cell("@cmd") == "'@cmd" and cell("-x") == "'-x"
    assert cell(-5) == "-5" and cell(Decimal("1.50")) == "1.50" and cell(True) == "true" and cell(None) == ""
    assert cell(datetime(2026, 11, 12, 9, 30)) == "2026-11-12T09:30:00"
    content = write_csv(["a", "b"], [["Olá; mundo", "=1"]])
    assert content.startswith(BOM.encode("utf-8"))
    assert content.decode("utf-8-sig") == 'a;b\r\n"Olá; mundo";\'=1\r\n'
    header, rows = read_csv(content)
    assert header == ["a", "b"] and list(rows) == [["Olá; mundo", "=1"]]
    header, rows = read_csv(b"a,b\n1,2\n")
    assert header == ["a", "b"] and list(rows) == [["1", "2"]]


@pytest.mark.needs_example_app
def test_export_contains_only_authorized_records_and_readable_fields(scenario):
    mine = scenario.create(scenario.a, owner=scenario.member, code="E-1", title='=HYPERLINK("x")', body="segredo")
    scenario.create(scenario.a, owner=scenario.admin_a, code="E-2", title="Privada do admin", visibility="private")
    scenario.create(scenario.b, owner=scenario.admin_b, code="B-1", title="Outra organização")
    client = login(scenario.member)
    response = client.post(export_url(scenario.a))
    assert response.status_code == 302 and response["Location"].endswith("/transfers/")
    run_pending_jobs()
    [transfer] = transfers(scenario.a)
    assert (transfer.state, transfer.row_count) == ("ready", 1)
    content = csv_content(transfer)
    header, rows = read_csv(content)
    rows = list(rows)
    assert "body" not in header  # Sensitive, never exported.
    assert {"sysid", "revision", "code", "title", "owner"} <= set(header)
    assert [row[header.index("code")] for row in rows] == ["E-1"]
    assert str(mine.pk) in content.decode("utf-8-sig")
    assert "'=HYPERLINK" in content.decode("utf-8-sig")  # Neutralized in the file.
    assert "segredo" not in content.decode("utf-8-sig") and "Outra organização" not in content.decode("utf-8-sig")
    # The requester is told the file is ready (the fan-out is its own job).
    from skeleton.notifications.models import NotificationRecipient

    run_pending_jobs()
    with system_context(reason="test:export-notify", organization_id=scenario.a.pk):
        assert NotificationRecipient.objects.filter(user=scenario.member).count() == 1


@pytest.mark.needs_example_app
def test_filters_of_the_list_apply_and_invalid_filters_are_refused(scenario):
    scenario.create(scenario.a, owner=scenario.member, code="F-1", title="Um")
    scenario.create(scenario.a, owner=scenario.member, code="F-2", title="Dois")
    client = login(scenario.member)
    assert client.post(export_url(scenario.a), {"code": "F-2"}).status_code == 302
    assert client.post(export_url(scenario.a), {"nope": "x"}).status_code == 400  # Not a list filter.
    assert client.post(export_url(scenario.a), {"body": "x"}).status_code == 400  # Sensitive field.
    run_pending_jobs()
    [transfer] = transfers(scenario.a)
    header, rows = read_csv(csv_content(transfer))
    assert [row[header.index("code")] for row in rows] == ["F-2"]


@pytest.mark.needs_example_app
def test_people_without_read_access_cannot_export(scenario):
    assert login(scenario.denied).post(export_url(scenario.a)).status_code == 404
    assert login(scenario.admin_b).post(export_url(scenario.a)).status_code == 404
    assert transfers(scenario.a) == []


@pytest.mark.needs_example_app
def test_only_the_requester_downloads_and_a_revocation_stops_it(scenario):
    scenario.create(scenario.a, owner=scenario.member, code="D-1", title="Baixar")
    client = login(scenario.member)
    client.post(export_url(scenario.a))
    run_pending_jobs()
    [transfer] = transfers(scenario.a)
    url = reverse("transfer:download", kwargs={"organization_id": scenario.a.pk, "sysid": transfer.pk})
    response = client.get(url)
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment;")
    assert response["X-Content-Type-Options"] == "nosniff" and response["Cache-Control"] == "no-store"
    assert login(scenario.admin_a).get(url).status_code == 404  # Even the administrator.
    assert login(scenario.admin_b).get(url).status_code == 404
    from skeleton.access.models import TablePermission

    with system_context(reason="test:export-revoke", organization_id=scenario.a.pk):
        TablePermission.objects.filter(table__model_label=TABLE, action="read").delete()
    assert client.get(url).status_code == 404


@override_settings(SKELETON_EXPORT_MAX_ROWS=1)
@pytest.mark.needs_example_app
def test_above_the_row_limit_the_export_fails_without_a_file(scenario):
    scenario.create(scenario.a, owner=scenario.member, code="L-1", title="Um")
    scenario.create(scenario.a, owner=scenario.member, code="L-2", title="Dois")
    login(scenario.member).post(export_url(scenario.a))
    run_pending_jobs()
    [transfer] = transfers(scenario.a)
    assert (transfer.state, transfer.error_code, transfer.file_key) == ("failed", "too_many_rows", "")


@pytest.mark.needs_example_app
def test_a_revocation_before_the_job_fails_closed(scenario):
    from skeleton.access.models import TablePermission

    scenario.create(scenario.a, owner=scenario.member, code="R-1", title="Um")
    login(scenario.member).post(export_url(scenario.a))
    with system_context(reason="test:export-revoke-early", organization_id=scenario.a.pk):
        TablePermission.objects.filter(table__model_label=TABLE, action="read").delete()
    run_pending_jobs()
    [transfer] = transfers(scenario.a)
    assert (transfer.state, transfer.file_key) == ("failed", "")


@pytest.mark.needs_example_app
def test_retention_deletes_the_row_and_the_file(scenario):
    from skeleton.files.storage import private_storage
    from skeleton.transfer.retention import clean_transfers

    scenario.create(scenario.a, owner=scenario.member, code="T-1", title="Um")
    login(scenario.member).post(export_url(scenario.a))
    run_pending_jobs()
    [transfer] = transfers(scenario.a)
    assert private_storage().exists(transfer.file_key)
    with system_context(reason="test:export-retention", organization_id=scenario.a.pk):
        assert clean_transfers(now=timezone.now()) == 0
        assert clean_transfers(now=timezone.now() + timedelta(days=8)) == 1
    assert transfers(scenario.a) == [] and not private_storage().exists(transfer.file_key)


@pytest.mark.needs_example_app
def test_the_list_page_shows_only_my_transfers(scenario):
    scenario.create(scenario.a, owner=scenario.member, code="P-1", title="Um")
    login(scenario.member).post(export_url(scenario.a))
    run_pending_jobs()
    url = reverse("transfer:list", kwargs={"organization_id": scenario.a.pk})
    assert "Baixar CSV" in login(scenario.member).get(url).content.decode()
    assert "Baixar CSV" not in login(scenario.admin_a).get(url).content.decode()
