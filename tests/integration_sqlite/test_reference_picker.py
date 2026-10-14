"""Reference selection authorizes the source and each tenant-scoped target."""
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from skeleton.accounts.services import bootstrap_identity
from skeleton.tenancy.context import tenant_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Reference-picker-test-password!72"


@pytest.fixture
def picker_actor():
    user, organization = bootstrap_identity(email="picker@example.test", organization="picker", name="Picker", password=PASSWORD)
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
    from examples.notes.models import Note
    with tenant_context(organization_id=organization.pk, actor=user, source="test:picker"):
        note = Note(organization=organization, owner=user, code="source", title="Source")
        note.save()
    return user, organization, note, client


def test_access_picker_is_bounded_and_does_not_list_foreign_identities(picker_actor):
    from skeleton.access.models import AccessGroup
    user, organization, note, client = picker_actor
    foreign, other = bootstrap_identity(email="foreign-picker@example.test", organization="foreign-picker", name="Foreign", password=PASSWORD)
    with tenant_context(organization_id=organization.pk, actor=user, source="test:groups"):
        for number in range(30):
            AccessGroup(organization=organization, code=f"g{number}", name=f"Group {number}").save()
    with tenant_context(organization_id=other.pk, actor=foreign, source="test:foreign-groups"):
        denied = AccessGroup(organization=other, code="hidden", name="Foreign secret group")
        denied.save()
    base = f"/api/v1/o/{organization.pk}/tables/example_notes.note/references/"
    owners = client.get(base + "owner/", {"record": str(note.pk), "access": "1"})
    assert owners.status_code == 200
    assert owners.json()["results"] == [{"sysid": str(user.pk), "label": str(user.pk)}]
    groups = client.get(base + "owning_group/", {"record": str(note.pk), "access": "1", "page_size": "10"})
    assert groups.status_code == 200
    data = groups.json()
    assert data["count"] == 30 and len(data["results"]) == 10
    assert str(denied.pk).encode() not in groups.content and b"Foreign secret" not in groups.content
    assert client.get(base + "owner/", {"access": "1"}).status_code == 400
    assert client.get(base + "owner/").status_code == 404
    assert client.get(base + "organization/", {"record": str(note.pk), "access": "1"}).status_code == 400
    assert client.get(base + "owning_group/", {"record": str(note.pk), "access": "1", "page_size": "101"}).status_code == 400
    assert client.get(base + "owner/", {"record": str(note.pk), "access": "1", "tenant": str(other.pk)}).status_code == 400
    assert client.get(base + "owner/", {"record": str(note.pk), "access": "1", "page": ["1", "2"]}).status_code == 400
    html = client.get(f"/o/{organization.pk}/tables/example_notes.note/references/owning_group/", {"record": str(note.pk), "access": "1"})
    assert html.status_code == 200 and b"Foreign secret" not in html.content


def test_picker_source_is_hidden_outside_tenant_and_blocked_when_disabled(picker_actor):
    from examples.notes.models import Note
    from skeleton.system.applications import configure_application
    user, organization, note, client = picker_actor
    foreign, other = bootstrap_identity(email="source-other@example.test", organization="source-other", name="Other", password=PASSWORD)
    with tenant_context(organization_id=other.pk, actor=foreign, source="test:foreign-source"):
        hidden = Note(organization=other, owner=foreign, code="foreign", title="Hidden")
        hidden.save()
    url = f"/api/v1/o/{organization.pk}/tables/example_notes.note/references/owner/"
    assert client.get(url, {"record": str(hidden.pk), "access": "1"}).status_code == 404
    with tenant_context(organization_id=organization.pk, actor=user, source="test:disable-picker"):
        configure_application("example_notes", user=user, enabled=False, configuration={}, revision=0)
    assert client.get(url, {"record": str(note.pk), "access": "1"}).status_code == 403


def test_record_page_reference_projection_has_constant_query_cost(picker_actor):
    from examples.notes.models import Note
    user, organization, _, client = picker_actor
    with tenant_context(organization_id=organization.pk, actor=user, source="test:many-owner-references"):
        for number in range(29):
            Note(organization=organization, owner=user, code=f"row-{number}", title=f"Row {number}").save()
    url = f"/api/v1/o/{organization.pk}/tables/example_notes.note/records/"
    with CaptureQueriesContext(connection) as small:
        first = client.get(url, {"page_size": "1"})
    with CaptureQueriesContext(connection) as large:
        page = client.get(url, {"page_size": "25"})
    assert first.status_code == page.status_code == 200
    assert len(page.json()["results"]) == 25
    assert {row["owner"] for row in page.json()["results"]} == {str(user.pk)}
    assert len(large) <= len(small) + 1


def test_persisted_unknown_configuration_blocks_release_check(picker_actor, monkeypatch):
    from dataclasses import replace
    from skeleton.system import app_registry
    from skeleton.system.app_registry import ConfigField
    from skeleton.system.models import AppInstallation
    user, organization, _, _ = picker_actor
    manifest = app_registry.get_app("example_notes")
    with monkeypatch.context() as old_release:
        old_release.setitem(app_registry._manifests, "example_notes",
                            replace(manifest, configuration_fields=(*manifest.configuration_fields, ConfigField("removed_field", str, ""))))
        with tenant_context(organization_id=organization.pk, actor=user, source="test:legacy-configuration"):
            AppInstallation(organization=organization, app_label="example_notes", enabled=True,
                            configuration={"removed_field": "must not be silently discarded"}).save()
    with pytest.raises(CommandError):
        call_command("check_application_configuration")


def test_owned_business_service_has_no_html_or_api_bypass(picker_actor):
    from django.core.exceptions import ValidationError
    from examples.notes.models import Note
    from skeleton.system.record_operations import RecordOperations, register_record_operations, unregister_record_operations
    user, organization, note, client = picker_actor

    def update(record, *, user, values):
        if not values.get("title", "").startswith("Approved "):
            raise ValidationError({"title": "Approval is required."})
        for name, value in values.items():
            setattr(record, name, value)
        record.save(update_fields=set(values))

    update.__module__ = Note.__module__
    register_record_operations("example_notes.note", RecordOperations(update=update))
    url = f"/api/v1/o/{organization.pk}/tables/example_notes.note/records/{note.pk}/"
    html = f"/o/{organization.pk}/tables/example_notes.note/{note.pk}/edit/"
    try:
        assert client.patch(url, {"title": "Rejected"}, content_type="application/json",
                            HTTP_IF_MATCH=f'"{note.revision}"').status_code == 400
        assert client.post(html, {"code": note.code, "title": "Rejected", "body": "",
                                  "revision": note.revision}).status_code == 400
        accepted = client.patch(url, {"title": "Approved through API"}, content_type="application/json",
                                HTTP_IF_MATCH=f'"{note.revision}"')
        assert accepted.status_code == 200
        revision = accepted.json()["revision"]
        assert revision == note.revision + 1
        assert client.post(html, {"code": note.code, "title": "Approved through HTML", "body": "",
                                  "revision": revision}).status_code == 302
        final = client.get(url)
        assert final.json()["title"] == "Approved through HTML"
        assert final.json()["revision"] == revision + 1
        assert client.delete(url, {}, content_type="application/json",
                             HTTP_IF_MATCH=final["ETag"]).status_code == 403
        assert client.get(f"/o/{organization.pk}/tables/example_notes.note/new/").status_code == 403
        assert client.get(url).json()["title"] == "Approved through HTML"
    finally:
        unregister_record_operations("example_notes")
