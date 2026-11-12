"""Record timeline tab (#185), PostgreSQL with the runtime role.

Audit and attachments in order, each item authorized for whoever looks: without
TablePermission ``audit`` only the attachments; changes cut to ``read_fields``; A/B.
"""

from dataclasses import replace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from skeleton.system import registry
from skeleton.system.registry import AttachmentPolicy
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


@pytest.fixture
def scenario(monkeypatch):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    monkeypatch.setitem(
        registry._registry,
        TABLE,
        replace(registry._registry[TABLE], attachments=AttachmentPolicy(max_bytes=1000, types=("application/pdf",))),
    )
    return build_service_scenario(Note, slug="timeline")


def _login(user):
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


@pytest.fixture
def history(scenario):
    """A note created, edited, given an attachment and edited again by the member."""
    from examples.notes.models import Note

    with acting_as(scenario.member, scenario.a):
        note = Note(
            organization=scenario.a, owner=scenario.member, code="T-1", title="Primeiro", visibility="organization"
        )
        note.save()
        note.title = "Segundo"
        note.save(update_fields={"title"})
    client = _login(scenario.member)
    uploaded = client.post(
        f"/api/v1/o/{scenario.a.pk}/tables/{TABLE}/records/{note.pk}/attachments/",
        {"file": SimpleUploadedFile("contrato.pdf", PDF)},
    )
    assert uploaded.status_code == 201, uploaded.content
    with acting_as(scenario.member, scenario.a):
        note = Note.objects.get(pk=note.pk)
        note.title = "Terceiro"
        note.save(update_fields={"title"})
    return note, client


def _url(scenario, note):
    return f"/o/{scenario.a.pk}/tables/{TABLE}/{note.pk}/"


def test_timeline_merges_audit_and_attachments_in_order(scenario, history, subtests):
    from skeleton.system.registry import get_spec
    from skeleton.web.timeline import record_timeline

    note, client = history
    with acting_as(scenario.member, scenario.a) as member:
        items, cursor = record_timeline(get_spec(TABLE), note, user=member)
    with subtests.test("order"):
        assert [item.kind for item in items] == ["audit", "attachment", "audit", "audit"] and cursor is None
        assert [item.data.get("operation") for item in items if item.kind == "audit"] == ["update", "update", "create"]
        assert [item.occurred_at for item in items] == sorted((item.occurred_at for item in items), reverse=True)
    with subtests.test("changes cut to read_fields"):
        changes = items[0].data["changes"]
        assert changes["title"]["new"] == "Terceiro"
        assert not {"updated_at", "revision", "updated_by_id", "organization_id"} & set(changes)
    with subtests.test("pages"):
        with acting_as(scenario.member, scenario.a) as member:
            first, cursor = record_timeline(get_spec(TABLE), note, user=member, page_size=3)
            rest, end = record_timeline(get_spec(TABLE), note, user=member, cursor=cursor, page_size=3)
        assert [item.sysid for item in first + rest] == [item.sysid for item in items] and end is None
    with subtests.test("page"):
        page = client.get(_url(scenario, note), {"view": "timeline"})
        assert page.status_code == 200
        assert b"contrato.pdf" in page.content and b"Terceiro" in page.content
        assert b'aria-current="page">Linha do tempo' in page.content
        assert client.get(_url(scenario, note)).status_code == 200
    with subtests.test("invalid parameters"):
        for params in ({"view": "x"}, {"view": "timeline", "cursor": "nope"}, {"cursor": "x"}, {"tab": "1"}):
            assert client.get(_url(scenario, note), params).status_code == 400


def test_without_audit_permission_only_attachments(scenario, history):
    from skeleton.access import services as access_services
    from skeleton.access.models import TablePermission

    note, client = history
    with acting_as(scenario.admin_a, scenario.a) as admin:
        for grant in TablePermission.objects.filter(table__model_label=TABLE, action="audit"):
            access_services.revoke_link(grant, user=admin)
    page = client.get(_url(scenario, note), {"view": "timeline"})
    assert page.status_code == 200
    assert b"contrato.pdf" in page.content
    assert b'data-kind="audit"' not in page.content and b"Terceiro" not in page.content.split(b"timeline-title")[1]


def test_admin_sees_audit_and_other_organization_sees_nothing(scenario, history):
    note, _ = history
    admin = _login(scenario.admin_a)
    page = admin.get(_url(scenario, note), {"view": "timeline"})
    assert page.content.count(b'data-kind="audit"') == 3 and page.content.count(b'data-kind="attachment"') == 1
    foreign = _login(scenario.admin_b)
    assert foreign.get(f"/o/{scenario.b.pk}/tables/{TABLE}/{note.pk}/", {"view": "timeline"}).status_code == 404
    denied = _login(scenario.denied)
    assert denied.get(_url(scenario, note), {"view": "timeline"}).status_code in (403, 404)
