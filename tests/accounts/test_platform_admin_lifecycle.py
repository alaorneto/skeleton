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
        demoted = update_platform_user(
            first, actor=first, data={"is_superuser": False}, expected_revision=fresh(first).revision
        )
        assert not demoted.is_superuser
        before = fresh(second)
        for data in ({"is_staff": False}, {"is_superuser": False}, {"is_active": False}):
            with pytest.raises(LastPlatformAdministrator):
                update_platform_user(second, actor=first, data=data, expected_revision=before.revision)
        for delete in (False, True):
            with pytest.raises(LastPlatformAdministrator):
                deactivate_user(second, actor=first, delete=delete)
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
        update_platform_user(second, actor=first, data={"is_staff": False}, expected_revision=fresh(second).revision)
    assert fresh(second).auth_version == version + 1
    assert list(active_platform_admins().values_list("pk", flat=True)) == [first.pk]


def test_superuser_without_staff_is_not_platform_admin(admins):
    from skeleton.access.policies import authorized_audit_queryset

    first, second = admins
    with request_context(actor=first, request_id=uuid4(), source="test:partial-platform-admin"):
        update_platform_user(second, actor=first, data={"is_staff": False}, expected_revision=fresh(second).revision)
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
        update_platform_user(second, actor=first, data={"is_staff": False}, expected_revision=fresh(second).revision)
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
    removal = {"expected_revision": fresh(first).revision}
    assert client.post(reverse("platform:user-delete", args=[first.pk]), removal).status_code == 409
    assert fresh(first).deleted_at is None


def _lifecycle_calls(target, organization, actor):
    from skeleton.accounts.services import restore_user, update_organization

    return {
        "update_platform_user": lambda: update_platform_user(
            target, actor=actor, data={"first_name": "Changed"}, expected_revision=fresh(target).revision
        ),
        "update_organization": lambda: update_organization(
            organization, actor=actor, data={"name": "Changed"}, expected_revision=organization.revision
        ),
        "deactivate_user": lambda: deactivate_user(target, actor=actor),
        "restore_user": lambda: restore_user(target, actor=actor),
    }


@pytest.mark.parametrize("service", ["update_platform_user", "update_organization", "deactivate_user", "restore_user"])
@pytest.mark.parametrize("flags", [(False, False), (True, False), (False, True)])
def test_lifecycle_services_refuse_a_non_platform_actor(admins, service, flags):
    from skeleton.tenancy.models import Organization

    first, _ = admins
    with system_context(reason="test:lifecycle-actor"):
        actor = create_identity(email="not-platform@example.test", password=PASSWORD)
        actor.is_staff, actor.is_superuser = flags
        actor.save(update_fields=["is_staff", "is_superuser"])
        target = create_identity(email="lifecycle-target@example.test", password=PASSWORD)
        organization = Organization.objects.get(slug="platform-first")
    before = fresh(target)
    with request_context(actor=actor, request_id=uuid4(), source="test:lifecycle-refused"):
        with pytest.raises(PermissionDenied):
            _lifecycle_calls(target, organization, actor)[service]()
    after = fresh(target)
    assert (after.revision, after.auth_version, after.is_active) == (before.revision, before.auth_version, True)
    with system_context(reason="test:lifecycle-org"):
        assert Organization.objects.get(pk=organization.pk).name == organization.name


@pytest.mark.parametrize("service", ["update_platform_user", "update_organization", "deactivate_user", "restore_user"])
def test_lifecycle_services_refuse_an_actor_that_is_not_the_context_actor(admins, service):
    from skeleton.tenancy.context import InvalidContext
    from skeleton.tenancy.models import Organization

    first, second = admins
    with system_context(reason="test:lifecycle-context"):
        target = create_identity(email="context-target@example.test", password=PASSWORD)
        organization = Organization.objects.get(slug="platform-first")
    with request_context(actor=first, request_id=uuid4(), source="test:lifecycle-other-actor"):
        with pytest.raises(InvalidContext):
            _lifecycle_calls(target, organization, second)[service]()


def test_lifecycle_services_accept_no_actor_only_under_system_context(admins):
    first, _ = admins
    with system_context(reason="test:lifecycle-none"):
        target = create_identity(email="none-target@example.test", password=PASSWORD)
    with request_context(actor=first, request_id=uuid4(), source="test:lifecycle-none"):
        with pytest.raises(PermissionDenied):
            deactivate_user(target, actor=None)
    assert fresh(target).is_active
    with pytest.raises(TypeError):
        deactivate_user(target)  # The actor is mandatory: forgetting it never falls back.


def test_deactivate_user_with_stale_revision_conflicts(admins):
    from skeleton.common.exceptions import ConcurrentUpdate

    first, _ = admins
    with system_context(reason="test:lifecycle-stale"):
        target = create_identity(email="stale-target@example.test", password=PASSWORD)
    stale = fresh(target).revision
    with request_context(actor=first, request_id=uuid4(), source="test:lifecycle-stale"):
        update_platform_user(target, actor=first, data={"first_name": "Moved"}, expected_revision=stale)
        with pytest.raises(ConcurrentUpdate):
            deactivate_user(target, actor=first, delete=True, expected_revision=stale)
    assert fresh(target).deleted_at is None and fresh(target).is_active
