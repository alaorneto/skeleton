import json
from uuid import uuid4

import pytest
from django.apps import apps
from django.test import Client, override_settings
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Disposable-integration-password-47!"


@pytest.fixture
def http_identity():
    user, organization = bootstrap_identity(email="http-admin@example.test", organization="http-a", name="HTTP A", password=PASSWORD)
    client = Client(enforce_csrf_checks=True)
    login = client.get(reverse("account_login"))
    assert login.status_code == 200
    response = client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert response.status_code == 302
    assert "sessionid" in client.cookies
    return client, user, organization


def send(client, method, url, data=None, revision=None, csrf=True):
    headers = {}
    if csrf:
        headers["HTTP_X_CSRFTOKEN"] = client.cookies["csrftoken"].value
    if revision is not None:
        headers["HTTP_IF_MATCH"] = revision
    return getattr(client, method)(url, data=json.dumps(data if data is not None else {}), content_type="application/json", **headers)


def notes_url(organization, sysid=None):
    kwargs = {"organization_id": organization.pk, "table_key": "example_notes.note"}
    if sysid:
        kwargs["sysid"] = sysid
    return reverse("api:record" if sysid else "api:records", kwargs=kwargs)


def require_notes():
    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")


def test_me_two_revision_cas_and_mass_assignment(http_identity):
    client, user, organization = http_identity
    url = reverse("api:me")
    initial = client.get(url)
    assert initial.status_code == 200
    data = initial.json()
    assert initial["ETag"] == f'"{data["revision"]}-{data["profile"]["revision"]}"'
    missing = send(client, "patch", url, {"first_name": "Ana"})
    assert missing.status_code == 428
    assert missing.json()["code"] == "revision_required"
    assert send(client, "patch", url, {"first_name": "Ana"}, 'W/"1"').status_code == 400
    forbidden = send(client, "patch", url, {"is_staff": True}, initial["ETag"])
    assert forbidden.status_code == 400
    edited = send(client, "patch", url, {"first_name": "Ana", "display_name": "Ana local"}, initial["ETag"])
    assert edited.status_code == 200
    assert edited.json()["first_name"] == "Ana"
    stale = send(client, "patch", url, {"last_name": "Must not win"}, initial["ETag"])
    assert stale.status_code == 412
    assert stale.json()["code"] == "revision_conflict"
    assert client.get(url).json()["last_name"] != "Must not win"


def test_session_csrf_and_anonymous_default_deny(http_identity):
    client, user, organization = http_identity
    url = reverse("api:me")
    revision = client.get(url)["ETag"]
    denied = send(client, "patch", url, {"first_name": "Blocked"}, revision, csrf=False)
    assert denied.status_code == 403
    assert Client().get(url).status_code == 403
    assert client.get(url).json()["first_name"] != "Blocked"


def test_note_crud_etag_soft_delete_restore(http_identity):
    require_notes()
    client, user, organization = http_identity
    url = notes_url(organization)
    created = send(client, "post", url, {"code": "http-note", "title": "Original", "body": "private audit body"})
    assert created.status_code == 201
    sysid = created.json()["sysid"]
    detail = notes_url(organization, sysid)
    retrieved = client.get(detail)
    assert retrieved.status_code == 200
    assert retrieved["ETag"] == created["ETag"]
    updated = send(client, "patch", detail, {"title": "Changed"}, retrieved["ETag"])
    assert updated.status_code == 200
    assert send(client, "patch", detail, {"title": "Loser"}, retrieved["ETag"]).status_code == 412
    assert send(client, "delete", detail, revision=updated["ETag"]).status_code == 204
    assert client.get(detail).status_code == 404
    assert client.get(url).json()["count"] == 0
    restore = reverse("api:restore", kwargs={"organization_id": organization.pk, "table_key": "example_notes.note", "sysid": sysid})
    revision = int(updated["ETag"].strip('"')) + 1
    restored = send(client, "post", restore, revision=f'"{revision}"')
    assert restored.status_code == 200
    assert restored.json()["sysid"] == sysid
    assert restored.json()["title"] == "Changed"
    events = client.get(reverse("api:audit", kwargs={"organization_id": organization.pk}), {"record_sysid": sysid}).json()
    assert "private audit body" not in json.dumps(events)
    assert {item["operation"] for item in events["results"]} >= {"create", "update", "soft_delete", "restore"}


def test_gateway_rejects_technical_keys_filters_and_hidden_access(http_identity):
    require_notes()
    client, user, organization = http_identity
    url = notes_url(organization)
    assert send(client, "post", url, {"code": "mass", "title": "Denied", "body": "", "owner": str(user.pk)}).status_code == 400
    assert send(client, "post", url, []).status_code == 400
    assert client.get(url, {"title__contains": "secret"}).status_code == 400
    assert client.get(url, {"page_size": 101}).status_code == 400
    technical = reverse("api:records", kwargs={"organization_id": organization.pk, "table_key": "sk_accounts.user"})
    assert client.get(technical).status_code == 404
    assert send(client, "patch", url, {"title": "no target"}, '"1"').status_code == 405


def test_cross_tenant_detail_and_access_do_not_resolve(http_identity):
    require_notes()
    client, user, organization = http_identity
    _, other = bootstrap_identity(email=user.email, organization="http-b", name="HTTP B")
    made = send(client, "post", notes_url(organization), {"code": "a-only", "title": "Tenant A", "body": ""})
    assert made.status_code == 201
    sysid = made.json()["sysid"]
    assert client.get(notes_url(other, sysid)).status_code == 404
    access = reverse("api:record-access", kwargs={"organization_id": other.pk, "table_key": "example_notes.note", "sysid": sysid})
    assert client.get(access).status_code == 404
    assert send(client, "patch", access, {"visibility": "organization"}, made["ETag"]).status_code == 404


def test_dedicated_role_group_crud_and_revision(http_identity):
    client, user, organization = http_identity
    def collection(resource):
        return reverse("api:access-list", kwargs={"organization_id": organization.pk, "resource": resource})
    role = send(client, "post", collection("roles"), {"code": "editor", "name": "Editor", "description": "HTTP role"})
    group = send(client, "post", collection("groups"), {"code": "review", "name": "Review", "description": "HTTP group"})
    assert role.status_code == group.status_code == 201
    link = send(client, "post", collection("group-roles"), {"group": group.json()["sysid"], "role": role.json()["sysid"]})
    assert link.status_code == 201
    link_url = reverse("api:access-detail", kwargs={"organization_id": organization.pk, "resource": "group-roles", "sysid": link.json()["sysid"]})
    assert send(client, "delete", link_url, revision=link["ETag"]).status_code == 204
    role_url = reverse("api:access-detail", kwargs={"organization_id": organization.pk, "resource": "roles", "sysid": role.json()["sysid"]})
    assert send(client, "patch", role_url, {"is_builtin": True}, role["ETag"]).status_code == 400
    edit = send(client, "patch", role_url, {"name": "New editor"}, role["ETag"])
    assert edit.status_code == 200
    assert send(client, "delete", role_url, revision=role["ETag"]).status_code == 412
    assert send(client, "delete", role_url, revision=edit["ETag"]).status_code == 204


def test_html_create_edit_revision_and_escaping(http_identity):
    require_notes()
    client, user, organization = http_identity
    kwargs = {"organization_id": organization.pk, "table_key": "example_notes.note"}
    records = reverse("web:records", kwargs=kwargs)
    create = reverse("web:record-create", kwargs=kwargs)
    page = client.get(create)
    assert page.status_code == 200
    assert page.context["cancel_url"] == records
    assert client.get(page.context["cancel_url"]).status_code == 200
    response = client.post(create, {"code": "html-note", "title": "<script>alert(1)</script>", "body": "Local HTML", "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert response.status_code == 302
    detail_url = response["Location"]
    detail = client.get(detail_url)
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in detail.content
    assert b"<script>alert(1)</script>" not in detail.content
    data = client.get(notes_url(organization)).json()["results"][0]
    sysid = data["sysid"]
    record_kwargs = {**kwargs, "sysid": sysid}
    edit = reverse("web:record-edit", kwargs=record_kwargs)
    audit_url = reverse("api:audit", kwargs={"organization_id": organization.pk})

    def snapshot():
        return client.get(notes_url(organization, sysid)).json(), client.get(audit_url, {"record_sysid": sysid}).json()

    before = snapshot()
    for route in ("web:record-edit", "web:record-access", "web:record-delete"):
        page = client.get(reverse(route, kwargs=record_kwargs))
        assert page.status_code == 200
        assert page.context["cancel_url"] == detail_url
        assert page.content.count(b'name="csrfmiddlewaretoken"') == 1
        assert page.content.count(b'name="revision"') == 1
        assert client.get(page.context["cancel_url"]).status_code == 200
    assert snapshot() == before
    post = {"code": "html-note", "title": "Winner", "body": "Updated", "revision": data["revision"], "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
    assert client.post(edit, post).status_code == 302
    post["title"] = "Loser"
    assert client.post(edit, post).status_code == 409
    current, events = snapshot()
    assert current["title"] == "Winner"
    assert current["sysid"] == sysid
    assert [event["operation"] for event in events["results"]].count("update") == 1
    delete = reverse("web:record-delete", kwargs=record_kwargs)
    page = client.get(delete)
    assert page.status_code == 200
    assert page.context["confirmation"]
    assert set(page.context["form"].fields) == {"revision"}
    for name in ("code", "title", "body"):
        assert f'name="{name}"'.encode() not in page.content
    confirmation = {"revision": current["revision"], "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
    assert client.post(delete, {**confirmation, "title": "Injected"}).status_code == 400
    assert snapshot() == (current, events)
    assert client.post(delete, {"revision": current["revision"]}).status_code == 403
    assert snapshot() == (current, events)
    deleted = client.post(delete, confirmation)
    assert deleted.status_code == 302
    assert deleted["Location"] == records
    assert client.get(detail_url).status_code == 404
    restore = reverse("web:record-restore", kwargs=record_kwargs)
    page = client.get(restore)
    assert page.status_code == 200
    assert page.context["cancel_url"] == reverse("web:deleted-records", kwargs=kwargs)
    assert set(page.context["form"].fields) == {"revision"}
    assert page.content.count(b'name="csrfmiddlewaretoken"') == 1
    assert page.content.count(b'name="revision"') == 1
    from examples.notes.models import Note
    from skeleton.audit.models import AuditEvent
    from tools.security_fixtures import actor_tenant
    with actor_tenant(user, organization):
        tombstone = Note.objects.with_deleted().get(pk=sysid)
        assert tombstone.deleted_at is not None
        assert tombstone.revision == current["revision"] + 1
        deleted_revision = tombstone.revision
        deleted_events = list(AuditEvent.objects.filter(record_sysid=sysid).values_list("pk", flat=True))
    assert client.get(page.context["cancel_url"]).status_code == 200
    assert client.post(restore, {"revision": deleted_revision, "title": "Injected", "csrfmiddlewaretoken": client.cookies["csrftoken"].value}).status_code == 400
    with actor_tenant(user, organization):
        assert Note.objects.with_deleted().get(pk=sysid).revision == deleted_revision
        assert list(AuditEvent.objects.filter(record_sysid=sysid).values_list("pk", flat=True)) == deleted_events
    restored = client.post(restore, {"revision": deleted_revision, "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert restored.status_code == 302
    assert restored["Location"] == detail_url
    current, events = snapshot()
    assert current["sysid"] == sysid
    assert current["title"] == "Winner"
    assert current["revision"] == deleted_revision + 1
    with actor_tenant(user, organization):
        assert Note.objects.get(pk=sysid).deleted_at is None
    operations = [event["operation"] for event in events["results"]]
    assert sorted(operations) == sorted(["create", "update", "soft_delete", "restore"])
    assert "Local HTML" not in json.dumps(events)
    assert "Updated" not in json.dumps(events)


def test_html_delete_and_edit_without_read_cancel_to_records(http_identity):
    require_notes()
    admin_client, admin, organization = http_identity
    from allauth.account.models import EmailAddress
    from skeleton.accounts.services import create_identity
    from skeleton.access.services import create_role, create_record_grant, grant_table_permission, provision_membership
    from skeleton.system.models import TableDefinition
    from skeleton.tenancy.context import system_context
    from tools.security_fixtures import actor_tenant

    created = send(admin_client, "post", notes_url(organization), {"code": "unreadable-code", "title": "Undisclosed title", "body": "Undisclosed body"})
    assert created.status_code == 201
    data = created.json()
    with system_context(reason="test:http-restricted-identity"):
        actor = create_identity(email="http-delete-only@example.test", password=PASSWORD)
        EmailAddress.objects.create(user=actor, email=actor.email, verified=True, primary=True)
    with actor_tenant(admin, organization):
        role = create_role(user=admin, data={"code": "unreadable-editor", "name": "Unreadable editor"})
    with system_context(reason="test:http-restricted-membership", organization_id=organization.pk):
        membership = provision_membership(organization=organization, user=actor, role=role)
    with actor_tenant(admin, organization):
        table = TableDefinition.objects.get(model_label="example_notes.note")
        for action in ("update", "delete"):
            grant_table_permission(user=admin, data={"role": role, "table": table, "action": action})
            create_record_grant(user=admin, data={"table": table, "record_sysid": data["sysid"], "membership": membership, "action": action})
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    assert client.post(reverse("account_login"), {"login": actor.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value}).status_code == 302
    kwargs = {"organization_id": organization.pk, "table_key": "example_notes.note", "sysid": data["sysid"]}
    assert client.get(reverse("web:record", kwargs=kwargs)).status_code == 403
    records = reverse("web:records", kwargs={"organization_id": organization.pk, "table_key": "example_notes.note"})
    audit_url = reverse("api:audit", kwargs={"organization_id": organization.pk})
    before_events = admin_client.get(audit_url, {"record_sysid": data["sysid"]}).json()
    for route in ("web:record-edit", "web:record-delete"):
        page = client.get(reverse(route, kwargs=kwargs))
        assert page.status_code == 200
        assert page.context["cancel_url"] == records
        assert page.content.count(b'name="csrfmiddlewaretoken"') == 1
        assert page.content.count(b'name="revision"') == 1
        if route == "web:record-delete":
            assert set(page.context["form"].fields) == {"revision"}
            for secret in ("unreadable-code", "Undisclosed title", "Undisclosed body"):
                assert secret.encode() not in page.content
            for name in ("code", "title", "body"):
                assert f'name="{name}"'.encode() not in page.content
        client.get(page.context["cancel_url"])
    assert admin_client.get(notes_url(organization, data["sysid"])).json() == data
    assert admin_client.get(audit_url, {"record_sysid": data["sysid"]}).json() == before_events
    delete = reverse("web:record-delete", kwargs=kwargs)
    confirmation = {"revision": data["revision"], "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
    assert client.post(delete, {**confirmation, "owner": str(actor.pk)}).status_code == 400
    assert admin_client.get(notes_url(organization, data["sysid"])).json() == data
    assert admin_client.get(audit_url, {"record_sysid": data["sysid"]}).json() == before_events
    assert client.post(delete, confirmation).status_code == 302
    assert admin_client.get(notes_url(organization, data["sysid"])).status_code == 404
    events = admin_client.get(audit_url, {"record_sysid": data["sysid"]}).json()
    assert sorted(event["operation"] for event in events["results"]) == ["create", "soft_delete"]


def test_mfa_enforcement_preserves_profile_escape_route(http_identity):
    client, user, organization = http_identity
    with override_settings(SKELETON_REQUIRE_ADMIN_MFA=True):
        denied = client.get(reverse("api:access-list", kwargs={"organization_id": organization.pk, "resource": "roles"}))
        assert denied.status_code == 403
        assert denied.json()["code"] == "mfa_required"
        assert client.get(reverse("web:profile")).status_code == 200


def test_access_management_is_separate_and_revision_protected(http_identity):
    require_notes()
    client, user, organization = http_identity
    created = send(client, "post", notes_url(organization), {"code": "access-note", "title": "ACL", "body": ""})
    assert created.status_code == 201
    sysid = created.json()["sysid"]
    detail = notes_url(organization, sysid)
    assert send(client, "patch", detail, {"visibility": "organization"}, created["ETag"]).status_code == 400
    access = reverse("api:record-access", kwargs={"organization_id": organization.pk, "table_key": "example_notes.note", "sysid": sysid})
    assert client.get(access).status_code == 200
    assert send(client, "patch", access, {"owner": str(uuid4())}, created["ETag"]).status_code == 400
    changed = send(client, "patch", access, {"visibility": "organization", "owning_group": None}, created["ETag"])
    assert changed.status_code == 200
    assert changed.json()["visibility"] == "organization"
    assert send(client, "patch", access, {"visibility": "private"}, created["ETag"]).status_code == 412
    assert client.get(detail).json()["visibility"] == "organization"


def test_duplicate_live_code_and_last_admin_return_conflict(http_identity):
    require_notes()
    client, user, organization = http_identity
    values = {"code": "unique-http", "title": "Existing", "body": ""}
    assert send(client, "post", notes_url(organization), values).status_code == 201
    duplicate = send(client, "post", notes_url(organization), values)
    assert duplicate.status_code == 409
    url = reverse("api:access-list", kwargs={"organization_id": organization.pk, "resource": "memberships"})
    memberships = client.get(url).json()["results"]
    own = next(row for row in memberships if row["user"] == str(user.pk))
    detail = reverse("api:access-detail", kwargs={"organization_id": organization.pk, "resource": "memberships", "sysid": own["sysid"]})
    denied = send(client, "delete", detail, revision=f'"{own["revision"]}"')
    assert denied.status_code == 409
    assert denied.json()["code"] == "last_administrator"
    assert client.get(detail).status_code == 200
