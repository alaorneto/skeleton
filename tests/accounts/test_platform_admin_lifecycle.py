"""Single platform administrator predicate and last platform administrator guard (#16)."""

from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse

from skeleton.accounts.models import User
from skeleton.accounts.platform import LastPlatformAdministrator
from skeleton.accounts.services import bootstrap_identity, create_identity, deactivate_user, update_platform_user
from skeleton.tenancy.context import current_context, request_context, system_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Platform-predicate-Password!16"


def active_platform_admins():
    return User.objects.filter(is_active=True, is_staff=True, is_superuser=True)


@pytest.fixture
def admins():
    first, _ = bootstrap_identity(
        email="platform-first@example.test", organization="platform-first", name="Platform first", password=PASSWORD
    )
    from allauth.account.models import EmailAddress

    with system_context(reason="test:second-platform-admin"):
        second = create_identity(email="platform-second@example.test", password=PASSWORD)
        EmailAddress.objects.create(user=second, email=second.email, verified=True, primary=True)
        second.is_staff = second.is_superuser = True
        second.save(update_fields=["is_staff", "is_superuser"])
    assert set(active_platform_admins().values_list("pk", flat=True)) == {first.pk, second.pk}
    return first, second


def fresh(user):
    return User.all_objects.get(pk=user.pk)


def test_demoting_or_removing_the_last_platform_admin_conflicts_without_writes(admins):
    # ``first`` also administers its organization; ``second`` has no membership, so only the
    # platform guard can refuse changes to it.
    first, second = admins
    with request_context(actor=first, request_id=uuid4(), source="test:last-platform-admin"):
        demoted = update_platform_user(first, data={"is_superuser": False}, expected_revision=fresh(first).revision)
        assert not demoted.is_superuser
        before = fresh(second)
        for data in ({"is_staff": False}, {"is_superuser": False}, {"is_active": False}):
            with pytest.raises(LastPlatformAdministrator):
                update_platform_user(second, data=data, expected_revision=before.revision)
        for delete in (False, True):
            with pytest.raises(LastPlatformAdministrator):
                deactivate_user(second, delete=delete)
    after = fresh(second)
    assert (after.revision, after.auth_version, after.is_active, after.deleted_at) == (
        before.revision,
        before.auth_version,
        True,
        None,
    )
    assert after.is_staff and after.is_superuser


def test_demoting_one_of_two_platform_admins_revokes_sessions(admins):
    first, second = admins
    version = fresh(second).auth_version
    with request_context(actor=first, request_id=uuid4(), source="test:demote-platform-admin"):
        update_platform_user(second, data={"is_staff": False}, expected_revision=fresh(second).revision)
    assert fresh(second).auth_version == version + 1
    assert list(active_platform_admins().values_list("pk", flat=True)) == [first.pk]


def test_superuser_without_staff_is_not_platform_admin(admins):
    from skeleton.access.policies import authorized_audit_queryset

    first, second = admins
    with request_context(actor=first, request_id=uuid4(), source="test:partial-platform-admin"):
        update_platform_user(second, data={"is_staff": False}, expected_revision=fresh(second).revision)
    partial = fresh(second)
    assert partial.is_superuser and not partial.is_staff
    assert partial.has_perm("sk_accounts.change_user") is False
    assert partial.has_module_perms("sk_accounts") is False
    assert fresh(first).has_perm("sk_accounts.change_user") is True
    with request_context(actor=partial, request_id=uuid4(), source="test:partial-guc"):
        assert current_context().platform_admin is False
        with pytest.raises(PermissionDenied):
            authorized_audit_queryset(partial)
    with request_context(actor=fresh(first), request_id=uuid4(), source="test:full-guc"):
        assert current_context().platform_admin is True
        assert authorized_audit_queryset(fresh(first)).model.__name__ == "AuditEvent"
    client = Client()
    assert client.post(reverse("account_login"), {"login": partial.email, "password": PASSWORD}).status_code == 302
    assert client.get(reverse("platform:overview")).status_code == 403
    assert client.get(reverse("platform:users")).status_code == 403


def test_platform_pages_use_predicate_and_refuse_last_admin_with_409(admins):
    first, second = admins
    client = Client()
    assert client.post(reverse("account_login"), {"login": first.email, "password": PASSWORD}).status_code == 302
    with request_context(actor=first, request_id=uuid4(), source="test:platform-pages"):
        update_platform_user(second, data={"is_staff": False}, expected_revision=fresh(second).revision)
    listing = client.get(reverse("platform:users")).content.decode()
    assert "Parcial" in listing
    detail = client.get(reverse("platform:user", args=[second.pk])).content.decode()
    assert "Administrador da plataforma" not in detail
    assert "Administrador da plataforma" in client.get(reverse("platform:user", args=[first.pk])).content.decode()
    revision = fresh(first).revision
    response = client.post(
        reverse("platform:user", args=[first.pk]),
        {"first_name": "", "last_name": "", "is_active": "on", "is_superuser": "on", "expected_revision": revision},
    )
    assert response.status_code == 409
    assert "A plataforma precisa manter ao menos um administrador ativo." in response.content.decode()
    assert fresh(first).is_staff and fresh(first).revision == revision
    assert client.post(reverse("platform:user-delete", args=[first.pk])).status_code == 409
    assert fresh(first).deleted_at is None
