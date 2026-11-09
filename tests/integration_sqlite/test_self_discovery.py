"""Self-discovery of own rows across tenants through an explicit helper (#18).

Runs on SQLite (Python filter) and, in the PostgreSQL job, with the runtime role
(RLS ``skeleton_self_*`` policies).
"""

import pytest
from django.db import connection

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.migration_operations import InstallRecordInfrastructure, self_discovery_mode
from skeleton.tenancy.context import InvalidContext, memberships_for_user, own_rows, system_context, tenant_context
from skeleton.tenancy.models import Membership

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Self-discovery-Password!72"


@pytest.fixture
def two_tenants():
    alice, alpha = bootstrap_identity(
        email="alice-discovery@example.test", organization="discovery-alpha", name="Alpha", password=PASSWORD
    )
    bob, beta = bootstrap_identity(
        email="bob-discovery@example.test", organization="discovery-beta", name="Beta", password=PASSWORD
    )
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership

    with system_context(reason="test:discovery-join", organization_id=beta.pk):
        provision_membership(organization=beta, user=alice, role=Role.objects.get(code="member"))
    return alice, alpha, bob, beta


def test_manager_stays_in_tenant_and_helper_reads_own_rows(two_tenants):
    alice, alpha, bob, beta = two_tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:discovery"):
        assert set(Membership.objects.values_list("organization_id", flat=True)) == {alpha.pk}
        mine = set(own_rows(Membership, alice).values_list("organization_id", flat=True))
        assert mine == {alpha.pk, beta.pk}
        # Bob's membership in Beta is not Alice's: never returned.
        assert not own_rows(Membership, alice).filter(user=bob).exists()
        with pytest.raises(InvalidContext):
            own_rows(Membership, bob)
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:discovery-list"):
        assert {row["organization_id"] for row in memberships_for_user(alice)} == {alpha.pk, beta.pk}


def test_models_without_declaration_are_refused(two_tenants):
    from skeleton.access.models import Role

    alice, alpha, _, _ = two_tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:discovery-refused"):
        with pytest.raises(InvalidContext):
            own_rows(Role, alice)


def test_cross_tenant_write_to_own_row_fails(two_tenants):
    alice, alpha, _, beta = two_tenants
    if connection.vendor != "postgresql":
        pytest.skip("Write isolation across tenants is enforced by PostgreSQL RLS.")
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:discovery-write"):
        foreign = own_rows(Membership, alice).get(organization_id=beta.pk)
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE skeleton.sk_tenancy_membership SET is_active=false, revision=revision+1 WHERE sysid=%s",
                [foreign.pk],
            )
            assert cursor.rowcount == 0
    with tenant_context(organization_id=beta.pk, actor=alice, source="test:discovery-after"):
        assert Membership.objects.get(pk=foreign.pk).is_active


def test_frozen_key_validation():
    from django.apps import apps

    model = apps.get_model("sk_tenancy", "Membership")
    assert self_discovery_mode(model, {"self_discovery_column": "user_id"}) == ("self", "user_id")
    assert self_discovery_mode(model, {}) == ("membership", "user_id")
    with pytest.raises(ValueError):
        self_discovery_mode(model, {"self_discovery_column": "organization_id"})
    with pytest.raises(ValueError):
        self_discovery_mode(model, {"self_discovery_column": "missing"})
    role = apps.get_model("sk_access", "Role")
    assert self_discovery_mode(role, {}) == (None, None)
    operation = InstallRecordInfrastructure("sk_tenancy", "Membership", {"self_discovery_column": "user_id"})
    assert operation.policy_names(model) == (
        "skeleton_self_select",
        "skeleton_self_insert",
        "skeleton_self_update",
        "skeleton_self_delete",
    )
    assert InstallRecordInfrastructure("sk_access", "Role", {}).policy_names(role) == ("skeleton_tenant",)
