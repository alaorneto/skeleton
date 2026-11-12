"""Limited platform roles: auditor and release operator (#34)."""

from uuid import uuid4

import pytest
from django.db import connection, transaction
from django.urls import reverse

from skeleton.accounts.platform import grant_platform_role, platform_capabilities, revoke_platform_role
from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.tenancy.context import platform_audit_scope, request_context, system_context
from tests import support

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Platform-roles-Password!34"


def person(email):
    from allauth.account.models import EmailAddress

    with system_context(reason="test:platform-role-person"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


def login(user):
    return support.login(user, password=PASSWORD, csrf=False)


@pytest.fixture
def setup():
    admin, organization = bootstrap_identity(
        email="roles-admin@example.test", organization="roles-org", name="Roles", password=PASSWORD
    )
    auditor = person("auditor@example.test")
    operator = person("operator@example.test")
    with request_context(actor=admin, request_id=uuid4(), source="test:platform-roles"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
        grant_platform_role(actor=admin, user=operator, role="operator")
    return {"admin": admin, "organization": organization, "auditor": auditor, "operator": operator}


def status(client, name, **kwargs):
    return client.get(reverse(name, kwargs=kwargs or None)).status_code


def test_capability_matrix(setup):
    auditor, operator = login(setup["auditor"]), login(setup["operator"])
    organization = setup["organization"]
    for name in ("platform:audit", "platform:catalog"):
        assert status(auditor, name) == 200
        assert status(operator, name) == 403
    assert status(operator, "platform:updates") == 200
    assert status(auditor, "platform:updates") == 403
    for name in ("platform:overview", "platform:users", "platform:organizations", "platform:apps", "platform:jobs"):
        assert status(auditor, name) == 403 and status(operator, name) == 403
    assert status(auditor, "platform:organization", sysid=organization.pk) == 403
    for client in (auditor, operator):
        assert client.get(reverse("web:dashboard", args=[organization.pk])).status_code == 404
    # The auditor reads the catalog but never edits it.
    from skeleton.system.models import TableDefinition

    table = TableDefinition.objects.order_by("db_table").first()
    page = auditor.get(reverse("platform:catalog-table", args=[table.pk]))
    assert page.status_code == 200
    edit = auditor.post(
        reverse("platform:catalog-table", args=[table.pk]),
        {"label": "Hijacked", "description": "", "expected_revision": table.revision},
    )
    assert edit.status_code == 403
    table.refresh_from_db()
    assert table.label != "Hijacked"
    home = auditor.get(reverse("web:home"))
    assert reverse("platform:audit").encode() in home.content


def test_auditor_reads_global_audit_only_inside_the_scope(setup):
    from skeleton.audit.models import AuditEvent

    auditor = setup["auditor"]
    page = login(auditor).get(reverse("platform:audit"))
    assert page.status_code == 200 and page.context["rows"].paginator.count > 0
    with request_context(actor=auditor, request_id=uuid4(), source="test:auditor-outside"):
        # Without the scope the runtime role sees no global events (RLS).
        assert AuditEvent.objects.filter(organization_id__isnull=True).count() == 0
        with platform_audit_scope(auditor):
            assert AuditEvent.objects.filter(organization_id__isnull=True).count() > 0
            with pytest.raises(Exception), transaction.atomic(), connection.cursor() as cursor:  # noqa: B017
                cursor.execute("UPDATE skeleton.sk_audit_auditevent SET source='x'")
    with request_context(actor=setup["operator"], request_id=uuid4(), source="test:operator-scope"):
        with pytest.raises(Exception):  # noqa: B017
            with platform_audit_scope(setup["operator"]):
                pass


def test_revocation_applies_on_the_next_request(setup):
    from skeleton.accounts.models import PlatformRoleAssignment

    admin, auditor = setup["admin"], setup["auditor"]
    client = login(auditor)
    assert status(client, "platform:audit") == 200
    version = type(auditor).all_objects.get(pk=auditor.pk).auth_version
    with request_context(actor=admin, request_id=uuid4(), source="test:platform-role-revoke"):
        assignment = PlatformRoleAssignment.objects.get(user=auditor)
        revoke_platform_role(actor=admin, assignment_id=assignment.pk, expected_revision=assignment.revision)
    assert type(auditor).all_objects.get(pk=auditor.pk).auth_version == version + 1
    response = client.get(reverse("platform:audit"))
    assert response.status_code in (302, 403)  # The session ended or the capability is gone.
    assert platform_capabilities(type(auditor).objects.get(pk=auditor.pk)) == frozenset()
    assert status(login(auditor), "platform:audit") == 403


def test_only_the_administrator_grants_roles(setup):
    from skeleton.accounts.models import PlatformRoleAssignment

    admin, auditor = setup["admin"], setup["auditor"]
    target = person("future-operator@example.test")
    url = reverse("platform:user-roles", args=[target.pk])
    assert login(auditor).post(url, {"role": "operator"}).status_code == 403
    assert not PlatformRoleAssignment.objects.filter(user=target).exists()
    assert login(admin).post(url, {"role": "operator"}).status_code == 302
    assert PlatformRoleAssignment.objects.filter(user=target, role="operator").exists()
    assert login(admin).post(url, {"role": "superuser"}).status_code == 302  # Refused with a message.
    assert not PlatformRoleAssignment.objects.filter(user=target, role="superuser").exists()


def test_schema_check_requires_the_auditor_policy(setup):
    import psycopg
    from psycopg import sql

    from skeleton.system.checks import check_deployed_schema
    from tools.integration_database import disposable_database_pair

    assert check_deployed_schema() == []
    pair = disposable_database_pair()
    with psycopg.connect(pair.owner, autocommit=True) as owner:
        owner.execute(sql.SQL("DROP POLICY skeleton_audit_select ON skeleton.sk_audit_auditevent"))
        owner.execute(
            "CREATE POLICY skeleton_audit_select ON skeleton.sk_audit_auditevent FOR SELECT TO skeleton_runtime "
            "USING ((organization_id = NULLIF(current_setting('skeleton.tenant_id', true), '')::uuid) "
            "OR (organization_id IS NULL AND current_setting('skeleton.platform_admin',true)='on'))"
        )
    assert any("sk_audit_auditevent" in error for error in check_deployed_schema())
