"""One dispatch for the access administration of both surfaces, and its new services (#265)."""

import pytest
from django.core.exceptions import PermissionDenied
from django.test import override_settings

from skeleton.access import resources
from skeleton.common.exceptions import ConcurrentUpdate
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.usefixtures("skeleton_db")


def test_dispatch_covers_every_dedicated_resource():
    for resource in resources.DEDICATED:
        assert resource in resources._CREATE or resource == "invitations", resource
        assert resource in resources.LINKS or resource == "invitations" or resource in resources._DELETE, resource
    assert set(resources._UPDATE) == {"memberships", "roles", "groups"}


@pytest.fixture
def fixture():
    return build_security_fixture()


def _invite(user, organization, email, monkeypatch):
    """Issue an invitation as ``user`` and return it with its token."""
    from skeleton.access.models import Role
    from skeleton.tenancy import invitations

    monkeypatch.setattr(invitations.secrets, "token_urlsafe", lambda size: f"token-{email:>40}")
    with actor_tenant(user, organization), override_settings(SKELETON_PUBLIC_URL="https://app.example.test"):
        role = Role.objects.get(code="member")
        return invitations.issue_invitation(user=user, email=email, role=role), f"token-{email:>40}"


def test_revoke_invitation_requires_capability_and_organization(fixture, monkeypatch):
    from skeleton.tenancy.invitations import InvalidInvitation, revoke_invitation, validate_invitation
    from skeleton.tenancy.models import Invitation

    invitation, token = _invite(fixture.admin, fixture.a, "guest@example.test", monkeypatch)
    with actor_tenant(fixture.alice, fixture.a), pytest.raises(PermissionDenied):
        revoke_invitation(Invitation.objects.get(pk=invitation.pk), user=fixture.alice)
    foreign, _ = _invite(fixture.bob, fixture.b, "foreign@example.test", monkeypatch)
    with actor_tenant(fixture.admin, fixture.a), pytest.raises(InvalidInvitation):
        revoke_invitation(foreign, user=fixture.admin)  # Another organization's invitation.
    with actor_tenant(fixture.admin, fixture.a):
        assert validate_invitation(fixture.a.pk, token).sysid == invitation.pk
        revoke_invitation(Invitation.objects.get(pk=invitation.pk), user=fixture.admin)
        assert Invitation.objects.with_deleted().get(pk=invitation.pk).deleted_at is not None
        with pytest.raises(InvalidInvitation):
            validate_invitation(fixture.a.pk, token)


def test_update_catalog_labels_compare_and_swap(fixture):
    from skeleton.system.catalog_labels import update_catalog_labels
    from skeleton.system.models import TableDefinition
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:catalog-labels"):
        table = TableDefinition.objects.order_by("sysid").first()
        data = {"label": table.label + " (editada)", "description": table.description}
        updated = update_catalog_labels(TableDefinition, table.pk, data, expected_revision=table.revision)
        assert (updated.label, updated.revision) == (data["label"], table.revision + 1)
        with pytest.raises(ConcurrentUpdate):
            update_catalog_labels(TableDefinition, table.pk, data, expected_revision=table.revision)
        same = update_catalog_labels(TableDefinition, table.pk, data, expected_revision=updated.revision)
        assert same.revision == updated.revision  # Nothing changed: nothing written.
