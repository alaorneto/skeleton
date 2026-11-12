"""Timeline of a record (#185): audit and attachments, each filtered for the reader."""

from dataclasses import replace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from skeleton.system import registry
from skeleton.system.registry import AttachmentPolicy
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from skeleton.web.timeline import timeline_for

pytestmark = pytest.mark.usefixtures("skeleton_db")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
KEY = "example_notes.note"


@pytest.fixture
def scenario(monkeypatch):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    monkeypatch.setitem(
        registry._registry,
        KEY,
        replace(registry._registry[KEY], attachments=AttachmentPolicy(max_bytes=500, types=("image/png",))),
    )
    return build_service_scenario(Note, slug="timeline")


def note_by(user, organization):
    from uuid import uuid4

    from examples.notes.models import Note

    with request_context(actor=user, request_id=uuid4(), source="test:timeline"):
        with tenant_context(organization_id=organization.pk, actor=user, source="test:timeline"):
            note = Note(organization=organization, owner=user, visibility="organization", code="T-1", title="Um")
            note.save()
            note.title = "Dois"
            note.save(update_fields={"title"})
    return note


def login(user):
    client = Client()
    assert client.post(reverse("account_login"), {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


def attach(client, organization, note):
    url = reverse("api:attachments", kwargs={"organization_id": organization.pk, "table_key": KEY, "sysid": note.pk})
    response = client.post(url, {"file": SimpleUploadedFile("foto.png", PNG)})
    assert response.status_code == 201, response.content


def detail_url(organization, note):
    return reverse("web:record", kwargs={"organization_id": organization.pk, "table_key": KEY, "sysid": note.pk})


def kinds(user, organization, note):
    from examples.notes.models import Note

    with request_context(actor=user, request_id=None, source="test:timeline-read"):
        with tenant_context(organization_id=organization.pk, actor=user, source="test:timeline-read"):
            record = Note.objects.get(pk=note.pk)
            return [(item.kind, item.title) for item in timeline_for(user, registry.get_spec(KEY), record)]


def test_timeline_joins_audit_and_attachments_most_recent_first(scenario):
    note = note_by(scenario.member, scenario.a)
    attach(login(scenario.member), scenario.a, note)
    items = kinds(scenario.admin_a, scenario.a, note)
    assert items[0] == ("attachment", "Anexo adicionado")
    assert [kind for kind, _ in items].count("audit") == 2
    page = login(scenario.admin_a).get(detail_url(scenario.a, note)).content.decode()
    assert "Linha do tempo" in page and "foto.png" in page and 'data-timeline-kind="audit"' in page


def test_without_the_audit_permission_only_the_attachments_appear(scenario):
    from skeleton.access.models import TablePermission

    note = note_by(scenario.member, scenario.a)
    attach(login(scenario.member), scenario.a, note)
    with system_context(reason="test:timeline-revoke-audit", organization_id=scenario.a.pk):
        TablePermission.objects.filter(table__model_label=KEY, action="audit").delete()
    assert {kind for kind, _ in kinds(scenario.member, scenario.a, note)} == {"attachment"}
    page = login(scenario.member).get(detail_url(scenario.a, note)).content.decode()
    assert 'data-timeline-kind="audit"' not in page and 'data-timeline-kind="attachment"' in page


def test_the_record_of_another_organization_has_no_timeline_for_outsiders(scenario):
    note = note_by(scenario.member, scenario.a)
    assert login(scenario.admin_b).get(detail_url(scenario.a, note)).status_code == 404
    assert login(scenario.denied).get(detail_url(scenario.a, note)).status_code == 403  # No table permission.
