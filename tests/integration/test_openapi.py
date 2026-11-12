"""GET /api/v1/openapi.json (#178): authenticated, filtered by actor, organization and token."""

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.integrations.tokens.services import issue_personal_token
from skeleton.tenancy.context import request_context, system_context
from tools.security_fixtures import build_security_fixture

pytestmark = pytest.mark.needs_example_app

NOTE = "example_notes.note"
RECORDS = "/api/v1/o/{organization_id}/tables/example_notes.note/records/"
RECORD = RECORDS + "{sysid}/"
META_SCHEMA = Path(__file__).resolve().parents[1] / "fixtures" / "openapi" / "oas-3.1-schema-2022-10-07.json"


@pytest.fixture
def access(skeleton_db):
    return build_security_fixture()


def session(user):
    client = Client()
    client.force_login(user)
    stored = client.session
    stored["skeleton_auth_version"] = user.auth_version
    stored.save()
    return client


def bearer(user, scopes):
    with request_context(actor=user, request_id=uuid4(), source="test:openapi"):
        issued = issue_personal_token(
            actor=user, name="OpenAPI", scopes=list(scopes), expires_at=timezone.now() + timedelta(days=7)
        )
    return Client(HTTP_AUTHORIZATION=f"Bearer {issued.text}")


def schema_of(client):
    response = client.get(reverse("api:openapi"))
    assert response.status_code == 200, response.content
    assert response["Content-Type"].startswith("application/json")
    assert "no-store" in response["Cache-Control"]
    return response.json()


def test_without_session_or_token_the_schema_is_refused(access):
    response = Client().get(reverse("api:openapi"))
    assert response.status_code in {401, 403}
    assert response.json()["code"] in {"not_authenticated", "permission_denied", "request_failed"}
    assert "paths" not in response.json()


def test_the_served_schema_is_valid_openapi_3_1(access):
    from jsonschema import Draft202012Validator

    document = schema_of(session(access.alice))
    meta = json.loads(META_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator(meta).validate(document)
    assert document["openapi"].startswith("3.1.")


def test_a_member_sees_the_table_operations_of_its_permissions_without_sensitive_fields(access):
    document = schema_of(session(access.alice))
    assert set(document["paths"][RECORDS]) == {"get", "post"}
    assert set(document["paths"][RECORD]) == {"get", "patch", "delete"}
    assert RECORD + "restore/" in document["paths"]
    record = document["components"]["schemas"][f"Record.{NOTE}"]
    assert {"sysid", "revision", "code", "title", "owner", "visibility", "owning_group"} <= set(record["properties"])
    text = json.dumps(document)
    assert '"body"' not in text  # sensitive_fields never appear, not even as a filter.
    for spec_field in ("token_digest", "storage_key", "secret_ciphertext", "digest", "password"):
        assert f'"{spec_field}"' not in text
    # A member without capabilities sees no access administration.
    assert "/api/v1/o/{organization_id}/roles/" not in document["paths"]
    assert "/api/v1/o/{organization_id}/webhooks/" not in document["paths"]
    assert "/api/v1/o/{organization_id}/apps/" not in document["paths"]


def test_an_administrator_also_sees_the_administration(access):
    document = schema_of(session(access.admin))
    for path in (
        "/api/v1/o/{organization_id}/roles/",
        "/api/v1/o/{organization_id}/memberships/{sysid}/",
        "/api/v1/o/{organization_id}/apps/",
        "/api/v1/o/{organization_id}/webhooks/",
        "/api/v1/o/{organization_id}/service-accounts/",
    ):
        assert path in document["paths"], path
    assert "/api/v1/o/{organization_id}/apps/example_notes/summary/" in document["paths"]


def test_default_deny_and_organizations_never_leak_tables(access):
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership
    from skeleton.accounts.services import create_identity

    with system_context(reason="test:openapi-identity"):
        carol = create_identity(email="carol@security.example.test", password="Fixture-only-password-2026!")
    with system_context(reason="test:openapi-b", organization_id=access.b.pk):
        provision_membership(organization=access.b, user=carol, role=Role.objects.get(code="member"))
    # Carol's role in B has no TablePermission: A's grant to its own "member" role does not apply.
    carol_paths = schema_of(session(carol))["paths"]
    assert not [path for path in carol_paths if "/tables/" in path]
    # Without any membership only the actor's own global routes remain.
    outsider = schema_of(session(access.outsider))["paths"]
    assert "/api/v1/me/" in outsider
    assert not [path for path in outsider if "/tables/" in path]


def test_revoking_the_table_permission_removes_the_table_at_once(access):
    from skeleton.access.models import TablePermission

    client = session(access.alice)
    assert RECORDS in schema_of(client)["paths"]
    with system_context(reason="test:openapi-revoke", organization_id=access.a.pk):
        for permission in TablePermission.objects.filter(table=access.table, role=access.member_role):
            permission.delete()
    assert RECORDS not in schema_of(client)["paths"]


def test_a_token_sees_only_its_scopes_and_token_methods(access):
    document = schema_of(bearer(access.alice, [f"{NOTE}:read"]))
    paths = document["paths"]
    assert set(paths[RECORDS]) == {"get"}
    assert set(paths[RECORD]) == {"get"}
    assert RECORD + "restore/" not in paths and RECORD + "access/" not in paths
    assert set(paths["/api/v1/me/"]) == {"get"}  # PATCH me is session-only.
    assert "/api/v1/me/tokens/" not in paths  # A token never manages tokens.
    assert not [path for path in paths if "/attachments/" in path]  # files:read is missing.
    assert paths[RECORDS]["get"]["security"] == [{"bearerAuth": []}, {"sessionAuth": []}]
    create_only = schema_of(bearer(access.alice, [f"{NOTE}:create"]))["paths"]
    assert set(create_only[RECORDS]) == {"post"}
    assert RECORD not in create_only


def test_an_administrator_token_never_sees_the_administration(access):
    paths = schema_of(bearer(access.admin, [f"{NOTE}:read"]))["paths"]
    assert not [path for path in paths if "/roles/" in path or "/webhooks/" in path or "/apps/" in path]
