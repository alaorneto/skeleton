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
    create = reverse("web:record-create", kwargs=kwargs)
    assert client.get(create).status_code == 200
    response = client.post(create, {"code": "html-note", "title": "<script>alert(1)</script>", "body": "Local HTML", "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert response.status_code == 302
    detail = client.get(response["Location"])
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in detail.content
    assert b"<script>alert(1)</script>" not in detail.content
    data = client.get(notes_url(organization)).json()["results"][0]
    edit = reverse("web:record-edit", kwargs={**kwargs, "sysid": data["sysid"]})
    post = {"code": "html-note", "title": "Winner", "body": "Updated", "revision": data["revision"], "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
    assert client.post(edit, post).status_code == 302
    post["title"] = "Loser"
    assert client.post(edit, post).status_code == 409
    assert client.get(notes_url(organization, data["sysid"])).json()["title"] == "Winner"


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
