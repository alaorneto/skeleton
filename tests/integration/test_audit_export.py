"""CSV export of the filtered audit log (#186): administrator only, same redaction as the page."""

import json
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from skeleton.transfer.csvio import read_csv

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    return build_service_scenario(Note, slug="audit-export")


def write(user, organization, code):
    from examples.notes.models import Note

    with request_context(actor=user, request_id=uuid4(), source="test:audit-export"):
        with tenant_context(organization_id=organization.pk, actor=user, source="test:audit-export"):
            note = Note(organization=organization, owner=user, visibility="organization", code=code, title="T")
            note.body = "segredo inicial"
            note.save()
            note.body = "segredo novo"
            note.save(update_fields={"body"})
    return note


def login(user):
    client = Client()
    assert client.post(reverse("account_login"), {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


def export(client, organization, data=None):
    return client.post(reverse("transfer:audit-export", kwargs={"organization_id": organization.pk}), data or {})


def latest(organization):
    from skeleton.transfer.models import TransferJob

    with system_context(reason="test:audit-export", organization_id=organization.pk):
        return TransferJob.objects.order_by("-created_at").first()


def content_of(transfer):
    from skeleton.files.storage import open_object

    with open_object(transfer.file_key) as stream:
        header, rows = read_csv(stream.read())
    return [dict(zip(header, row, strict=True)) for row in rows]


def test_administrator_exports_the_filtered_audit_with_redaction(scenario):
    note = write(scenario.member, scenario.a, "X-1")
    write(scenario.admin_b, scenario.b, "B-1")
    client = login(scenario.admin_a)
    assert export(client, scenario.a, {"operation": "update", "model_label": "example_notes.note"}).status_code == 302
    run_pending_jobs()
    transfer = latest(scenario.a)
    assert (transfer.state, transfer.row_count) == ("ready", 1)
    [row] = content_of(transfer)
    assert (row["operation"], row["record_sysid"]) == ("update", str(note.pk))
    changes = json.loads(row["changes"])
    assert changes["body"]["new"] == "[REDACTED]" and "segredo" not in row["changes"]
    url = reverse("transfer:download", kwargs={"organization_id": scenario.a.pk, "sysid": transfer.pk})
    assert client.get(url).status_code == 200
    assert login(scenario.member).get(url).status_code == 404


def test_only_the_administrator_of_the_organization_exports(scenario):
    assert export(login(scenario.member), scenario.a).status_code == 404
    assert export(login(scenario.admin_b), scenario.a).status_code == 404
    assert latest(scenario.a) is None


def test_invalid_filters_are_refused_before_the_job(scenario):
    assert export(login(scenario.admin_a), scenario.a, {"operation": "truncate"}).status_code == 400
    assert latest(scenario.a) is None


def test_the_audit_page_offers_the_export_only_to_the_administrator(scenario):
    url = reverse("web:audit", kwargs={"organization_id": scenario.a.pk})
    action = reverse("transfer:audit-export", kwargs={"organization_id": scenario.a.pk})
    assert action in login(scenario.admin_a).get(url).content.decode()
    assert action not in login(scenario.member).get(url).content.decode()
