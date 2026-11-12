"""Access administration of organization A never reads or links identifiers of organization B (#263)."""

import json

import pytest
from django.db import connection
from django.test import Client
from django.urls import reverse

from tools.security_fixtures import actor_tenant, build_security_fixture

PASSWORD = "Fixture-only-password-2026!"


@pytest.fixture
def access(skeleton_db):
    from skeleton.access.models import Role
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Membership

    assert connection.vendor == "postgresql"
    fixture = build_security_fixture()
    with system_context(reason="test:foreign-ids", organization_id=fixture.b.pk):
        foreign_role = Role.objects.get(code="member")
        foreign_membership = Membership.objects.get(user=fixture.alice)
    return fixture, foreign_role, foreign_membership


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def post(client, url, data):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    return client.post(url, data=json.dumps(data), content_type="application/json", **headers)


def link_counts(fixture):
    from skeleton.access.models import GroupMember, GroupRole, MemberRole, RecordGrant, TablePermission
    from skeleton.tenancy.context import system_context

    models = (MemberRole, GroupMember, GroupRole, TablePermission, RecordGrant)
    with actor_tenant(fixture.admin, fixture.a):
        in_a = [model.objects.count() for model in models]
    with system_context(reason="test:foreign-counts", organization_id=fixture.b.pk):
        in_b = [model.objects.count() for model in models]
    return in_a, in_b


def test_detail_of_foreign_role_and_group_is_not_found(access):
    fixture, foreign_role, _ = access
    client = login(fixture.admin)
    for resource, sysid in (("roles", foreign_role.pk), ("groups", fixture.foreign_group.pk)):
        url = reverse(
            "api:access-detail", kwargs={"organization_id": fixture.a.pk, "resource": resource, "sysid": sysid}
        )
        assert client.get(url).status_code == 404, resource


def _bodies(fixture, foreign_role, foreign_membership):
    with actor_tenant(fixture.admin, fixture.a):
        from skeleton.access.models import Role

        own_role = Role.objects.get(code="member")
    bob, group, table = str(fixture.bob_membership.pk), str(fixture.group.pk), str(fixture.table.pk)
    role_b, membership_b, group_b = str(foreign_role.pk), str(foreign_membership.pk), str(fixture.foreign_group.pk)
    return [
        ("member-roles", {"membership": bob, "role": role_b}),
        ("member-roles", {"membership": membership_b, "role": str(own_role.pk)}),
        ("group-members", {"group": group_b, "membership": bob}),
        ("group-roles", {"group": group_b, "role": str(own_role.pk)}),
        ("group-roles", {"group": group, "role": role_b}),
        ("table-permissions", {"role": role_b, "table": table, "action": "read"}),
        (
            "record-grants",
            {"table": table, "record_sysid": str(fixture.foreign.pk), "membership": bob, "action": "read"},
        ),
    ]


def test_links_never_accept_foreign_identifiers(access):
    fixture, foreign_role, foreign_membership = access
    before = link_counts(fixture)
    client = login(fixture.admin)
    # B's role shares the builtin code and name with A's; the group and the note are B's alone.
    leaked = (fixture.foreign_group.code, fixture.foreign_group.name, fixture.foreign.title)
    for resource, body in _bodies(fixture, foreign_role, foreign_membership):
        url = reverse("api:access-list", kwargs={"organization_id": fixture.a.pk, "resource": resource})
        response = post(client, url, body)
        assert response.status_code == 400, (resource, body, response.content)
        text = response.content.decode()
        assert not any(value in text for value in leaked), (resource, text)
    assert link_counts(fixture) == before


def test_html_access_form_rejects_foreign_identifier(access):
    fixture, foreign_role, _ = access
    before = link_counts(fixture)
    client = login(fixture.admin)
    url = reverse("web:access-list", kwargs={"organization_id": fixture.a.pk, "resource": "member-roles"})
    response = client.post(
        url,
        {
            "membership": str(fixture.bob_membership.pk),
            "role": str(foreign_role.pk),
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
        },
    )
    assert response.status_code == 200  # The form is shown again with the error, not a success redirect.
    assert link_counts(fixture) == before
