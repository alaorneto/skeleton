"""Consumer regressions for RD05, SBR01 and SBR04 (both database fixtures)."""

import json
import re
from urllib.parse import urlsplit

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from django.urls import reverse

from tests import support
from tools.security_fixtures import actor_tenant, build_security_fixture

PASSWORD = "Fixture-only-password-2026!"


@pytest.fixture
def access(skeleton_db):
    return build_security_fixture()


def login(user):
    return support.login(user, password=PASSWORD)


def membership_url(access, detail=False):
    kwargs = {"organization_id": access.a.pk, "resource": "memberships"}
    if detail:
        kwargs["sysid"] = access.bob_membership.pk
    return reverse("api:access-detail" if detail else "api:access-list", kwargs=kwargs)


def mutate(client, method, url, data=None, revision=None):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    if revision is not None:
        headers["HTTP_IF_MATCH"] = f'"{revision}"'
    return getattr(client, method)(url, data=json.dumps(data or {}), content_type="application/json", **headers)


@pytest.mark.parametrize("method", ["post", "patch"])
def test_rd05_post_and_patch_revoke_all_membership_acl(access, method):
    from skeleton.access.models import GroupMember, MemberRole, RecordGrant
    from skeleton.access.services import create_record_grant
    from skeleton.tenancy.models import Membership

    with actor_tenant(access.admin, access.a):
        create_record_grant(
            user=access.admin,
            data={
                "table": access.table,
                "record_sysid": access.private.pk,
                "membership": access.bob_membership,
                "action": "read",
            },
        )
        links = {
            model: list(model.objects.filter(membership=access.bob_membership).values_list("pk", flat=True))
            for model in (MemberRole, GroupMember, RecordGrant)
        }
        assert all(links.values())
    client = login(access.admin)
    data = {"is_active": False}
    if method == "post":
        data["user"] = str(access.bob.pk)
    response = mutate(
        client, method, membership_url(access, detail=method == "patch"), data, access.bob_membership.revision
    )
    assert response.status_code == (201 if method == "post" else 200)
    with actor_tenant(access.admin, access.a):
        membership = Membership.objects.get(pk=access.bob_membership.pk)
        assert not membership.is_active
        for model, ids in links.items():
            assert not model.objects.filter(pk__in=ids).exists()
            assert model.objects.with_deleted().filter(pk__in=ids, deleted_at__isnull=False).count() == len(ids)
    activated = mutate(client, "post", membership_url(access), {"user": str(access.bob.pk), "is_active": True})
    assert activated.status_code == 201
    with actor_tenant(access.admin, access.a):
        for model, ids in links.items():
            assert not model.objects.filter(pk__in=ids).exists()


def test_rd05_patch_stale_revision_and_last_admin_remain_protected(access):
    from skeleton.tenancy.models import Membership

    client = login(access.admin)
    revision = access.bob_membership.revision
    assert mutate(client, "patch", membership_url(access, True), {"is_active": False}, revision).status_code == 200
    assert mutate(client, "patch", membership_url(access, True), {"is_active": True}, revision).status_code == 412
    assert (
        mutate(client, "post", membership_url(access), {"user": str(access.admin.pk), "is_active": False}).status_code
        == 409
    )
    with actor_tenant(access.admin, access.a):
        assert Membership.objects.get(user=access.admin).is_active
        assert not Membership.objects.get(pk=access.bob_membership.pk).is_active


@pytest.mark.parametrize("active", [True, False])
def test_sbr01_membership_post_restore_persists_requested_state_once(access, active):
    from skeleton.access.services import delete_membership
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.models import Membership

    with actor_tenant(access.admin, access.a):
        delete_membership(access.bob_membership, user=access.admin)
        previous = Membership.objects.with_deleted().get(pk=access.bob_membership.pk)
        revision = previous.revision
        events = AuditEvent.objects.filter(record_sysid=previous.pk).count()
    client = login(access.admin)
    response = mutate(client, "post", membership_url(access), {"user": str(access.bob.pk), "is_active": active})
    assert response.status_code == 201
    with actor_tenant(access.admin, access.a):
        restored = Membership.objects.get(pk=previous.pk)
        assert restored.is_active is active
        assert response.json()["is_active"] is restored.is_active
        assert restored.revision == revision + 1
        assert AuditEvent.objects.filter(record_sysid=restored.pk).count() == events + 1


def test_sbr01_reinvitation_restores_same_membership_and_consumes_once(access):
    from skeleton.access.models import MemberRole
    from skeleton.access.services import delete_membership
    from skeleton.tenancy.invitations import InvalidInvitation, accept_invitation, issue_invitation
    from skeleton.tenancy.models import Invitation, Membership

    with actor_tenant(access.admin, access.a):
        delete_membership(access.bob_membership, user=access.admin)
        revision = Membership.objects.with_deleted().get(pk=access.bob_membership.pk).revision
        invitation = issue_invitation(
            user=access.admin,
            email=access.bob.email,
            role=access.member_role,
            request=RequestFactory().get("/", HTTP_HOST="testserver"),
        )
    message = next(message for message in reversed(mail.outbox) if invitation.email in message.to)
    path = urlsplit(re.search(r"https?://[^\s]+", message.body).group()).path
    token = path.rstrip("/").rsplit("/", 1)[-1]
    result = accept_invitation(user=access.bob, organization_id=access.a.pk, token=token)
    assert result.pk == access.bob_membership.pk
    with actor_tenant(access.admin, access.a):
        membership = Membership.objects.get(pk=result.pk)
        assert membership.is_active
        assert membership.revision == revision + 1
        assert MemberRole.objects.filter(membership=membership, role=access.member_role).count() == 1
        accepted = Invitation.objects.get(pk=invitation.pk)
        assert accepted.accepted_at is not None and accepted.accepted_by == access.bob.pk
        accepted_revision = accepted.revision
    with pytest.raises(InvalidInvitation):
        accept_invitation(user=access.bob, organization_id=access.a.pk, token=token)
    with actor_tenant(access.admin, access.a):
        assert Invitation.objects.get(pk=invitation.pk).revision == accepted_revision
        assert MemberRole.objects.filter(membership_id=result.pk, role=access.member_role).count() == 1


def test_sbr04_inactive_membership_can_be_deleted_without_cross_tenant_bypass(access):
    from skeleton.access.services import delete_membership
    from skeleton.tenancy.models import Membership

    with actor_tenant(access.bob, access.b):
        foreign = Membership.objects.get(user=access.alice)
    client = login(access.admin)
    response = mutate(
        client, "patch", membership_url(access, True), {"is_active": False}, access.bob_membership.revision
    )
    assert response.status_code == 200
    deleted = mutate(client, "delete", membership_url(access, True), revision=response.json()["revision"])
    assert deleted.status_code == 204
    with actor_tenant(access.admin, access.a):
        record = Membership.objects.with_deleted().get(pk=access.bob_membership.pk)
        assert record.deleted_at is not None and not record.is_active
        with pytest.raises(ValidationError):
            delete_membership(foreign, user=access.admin)
    with actor_tenant(access.bob, access.b):
        assert Membership.objects.get(pk=foreign.pk).is_active


# --------------------------------------------------------------------------- leaving an organization (#25)


def leave_url(organization, api=False):
    if api:
        return reverse("api:leave-organization", kwargs={"organization_id": organization.pk})
    return reverse("web:leave-organization", kwargs={"organization_id": organization.pk})


def _links(membership):
    from skeleton.access.models import GroupMember, MemberRole, RecordGrant

    return {
        model: list(model.objects.with_deleted().filter(membership=membership).values_list("pk", flat=True))
        for model in (MemberRole, GroupMember, RecordGrant)
    }


def test_member_leaves_and_loses_direct_and_group_access_on_the_next_request(access):
    from skeleton.access.services import create_record_grant
    from skeleton.tenancy.models import Membership

    with actor_tenant(access.admin, access.a):
        create_record_grant(
            user=access.admin,
            data={
                "table": access.table,
                "record_sysid": access.private.pk,
                "membership": access.bob_membership,
                "action": "read",
            },
        )
        links = _links(access.bob_membership)
    assert all(links.values())  # A direct role, a group and a record grant.
    client = login(access.bob)
    dashboard = reverse("web:dashboard", kwargs={"organization_id": access.a.pk})
    assert client.get(dashboard).status_code == 200
    page = client.get(leave_url(access.a))
    assert page.status_code == 200 and page.context["revision"] == access.bob_membership.revision
    with actor_tenant(access.admin, access.a):
        assert Membership.objects.get(pk=access.bob_membership.pk).is_active  # GET changes nothing.
    token = client.cookies["csrftoken"].value
    response = client.post(leave_url(access.a), {"revision": page.context["revision"], "csrfmiddlewaretoken": token})
    assert response.status_code == 302 and response.url == reverse("web:home")
    assert client.get(dashboard).status_code == 404
    with actor_tenant(access.admin, access.a):
        left = Membership.objects.with_deleted().get(pk=access.bob_membership.pk)
        assert left.deleted_at is not None and not left.is_active
        assert not Membership.objects.filter(user=access.bob).exists()
        for model, ids in links.items():
            assert not model.objects.filter(pk__in=ids).exists()
            assert model.objects.with_deleted().filter(pk__in=ids, deleted_at__isnull=False).count() == len(ids)
    # Leaving A does not touch B, where Bob still administers.
    assert client.get(reverse("web:dashboard", kwargs={"organization_id": access.b.pk})).status_code == 200


def test_leaving_requires_csrf_and_a_current_revision(access):
    from skeleton.tenancy.models import Membership

    client = login(access.alice)
    revision = access.alice_membership.revision
    assert client.post(leave_url(access.a), {"revision": revision}).status_code == 403
    token = client.cookies["csrftoken"].value
    assert client.post(leave_url(access.a), {"revision": revision + 1, "csrfmiddlewaretoken": token}).status_code == 409
    assert client.post(leave_url(access.a), {"csrfmiddlewaretoken": token}).status_code == 409
    with actor_tenant(access.admin, access.a):
        assert Membership.objects.get(pk=access.alice_membership.pk).is_active


def test_last_administrator_cannot_leave_and_nothing_changes(access):
    from skeleton.access.models import MemberRole
    from skeleton.tenancy.models import Membership

    with actor_tenant(access.admin, access.a):
        membership = Membership.objects.get(user=access.admin)
        before = (membership.revision, MemberRole.objects.filter(membership=membership).count())
    client = login(access.admin)
    token = client.cookies["csrftoken"].value
    response = client.post(leave_url(access.a), {"revision": before[0], "csrfmiddlewaretoken": token})
    assert response.status_code == 409
    assert "última pessoa que administra" in response.content.decode()
    api = mutate(client, "post", leave_url(access.a, api=True), revision=before[0])
    assert api.status_code == 409 and api.json()["code"] == "last_administrator"
    with actor_tenant(access.admin, access.a):
        membership = Membership.objects.get(user=access.admin)
        assert (membership.revision, MemberRole.objects.filter(membership=membership).count()) == before
    assert client.get(reverse("web:dashboard", kwargs={"organization_id": access.a.pk})).status_code == 200


def test_api_leave_uses_the_membership_etag(access):
    from skeleton.tenancy.models import Membership

    client = login(access.alice)
    own = client.get(reverse("api:own-membership", kwargs={"organization_id": access.a.pk}))
    assert own.status_code == 200 and own.json()["sysid"] == str(access.alice_membership.pk)
    url = leave_url(access.a, api=True)
    assert mutate(client, "post", url).status_code == 428
    assert mutate(client, "post", url, revision=own.json()["revision"] + 1).status_code == 412
    assert mutate(client, "post", url, {"user": str(access.bob.pk)}, own.json()["revision"]).status_code == 400
    assert client.get(url).status_code == 405
    response = client.post(url, content_type="application/json", HTTP_IF_MATCH=own["ETag"])
    assert response.status_code == 403  # CSRF: the session API still requires the token.
    assert mutate(client, "post", url, revision=own.json()["revision"]).status_code == 204
    assert client.get(reverse("api:own-membership", kwargs={"organization_id": access.a.pk})).status_code == 404
    with actor_tenant(access.admin, access.a):
        assert Membership.objects.with_deleted().get(pk=access.alice_membership.pk).deleted_at is not None
        # Bob's membership in A is untouched by Alice leaving.
        assert Membership.objects.get(pk=access.bob_membership.pk).is_active


def test_administrator_leaves_when_another_administrator_remains(access):
    from skeleton.access.models import AccessGroup, GroupMember, GroupRole, Role
    from skeleton.access.services import LastAdministrator, leave_organization
    from skeleton.tenancy.models import Membership

    with actor_tenant(access.admin, access.a):
        admins = AccessGroup(organization=access.a, code="admins", name="Admins")
        admins.save()
        GroupRole(organization=access.a, group=admins, role=Role.objects.get(code="organization_admin")).save()
        GroupMember(organization=access.a, group=admins, membership=access.alice_membership).save()
    # Alice administers only through a group; the direct administrator may now leave.
    with actor_tenant(access.admin, access.a):
        leave_organization(access.admin)
    with actor_tenant(access.alice, access.a):
        assert not Membership.objects.filter(user=access.admin).exists()
        with pytest.raises(LastAdministrator):
            leave_organization(access.alice)
        assert Membership.objects.get(pk=access.alice_membership.pk).is_active


def test_leave_service_refuses_another_actor(access):
    from django.core.exceptions import PermissionDenied

    from skeleton.access.services import leave_organization

    with actor_tenant(access.admin, access.a):
        with pytest.raises(PermissionDenied):
            leave_organization(access.alice)


def test_invitation_readmits_who_left_with_the_same_membership(access):
    from skeleton.access.models import Role
    from skeleton.access.services import leave_organization
    from skeleton.tenancy.invitations import accept_invitation, issue_invitation
    from skeleton.tenancy.models import Membership

    with actor_tenant(access.alice, access.a):
        leave_organization(access.alice)
    with actor_tenant(access.admin, access.a):
        invitation = issue_invitation(
            user=access.admin,
            email=access.alice.email,
            role=Role.objects.get(code="member"),
            request=RequestFactory().get("/", HTTP_HOST="testserver"),
        )
    message = next(message for message in reversed(mail.outbox) if invitation.email in message.to)
    path = urlsplit(re.search(r"https?://[^\s]+", message.body).group()).path
    accept_invitation(user=access.alice, organization_id=access.a.pk, token=path.rstrip("/").rsplit("/", 1)[-1])
    with actor_tenant(access.alice, access.a):
        assert Membership.objects.get(user=access.alice).pk == access.alice_membership.pk


def test_home_without_membership_shows_the_join_paths(access):
    from skeleton.tenancy.invitations import issue_invitation

    client = login(access.outsider)
    home = client.get(reverse("web:home")).content.decode()
    assert "Você ainda não pertence a uma organização." in home
    assert "abra o link de um convite" in home and "Convite pendente" not in home
    with actor_tenant(access.admin, access.a):
        invitation = issue_invitation(
            user=access.admin,
            email=access.outsider.email,
            role=access.member_role,
            request=RequestFactory().get("/", HTTP_HOST="testserver"),
        )
    message = next(message for message in reversed(mail.outbox) if invitation.email in message.to)
    assert client.get(urlsplit(re.search(r"https?://[^\s]+", message.body).group()).path).status_code == 200
    home = client.get(reverse("web:home")).content.decode()
    assert "Convite pendente" in home and access.outsider.email in home
    assert access.a.name not in home  # The block never reveals the organization before acceptance.
