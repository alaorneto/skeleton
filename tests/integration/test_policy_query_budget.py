"""Each authorization decision resolves the membership once, and nothing is cached across decisions (#227)."""

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test.utils import CaptureQueriesContext

from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app
# Membership, TableDefinition, TablePermission and the administrator EXISTS.
DECISION_BUDGET = 4


@pytest.fixture
def access(skeleton_db):
    return build_security_fixture()


def app_state_queries(user, organization):
    """What require_application_enabled costs on its own: subtracted from the budgets."""
    from skeleton.system.applications import require_application_enabled

    with actor_tenant(user, organization), CaptureQueriesContext(connection) as captured:
        require_application_enabled("example_notes")
    return len(captured)


def decision(user, organization, call):
    with actor_tenant(user, organization), CaptureQueriesContext(connection) as captured:
        call()
    return captured


def membership_reads(captured):
    return [query["sql"] for query in captured if 'FROM "sk_tenancy_membership"' in query["sql"].split("WHERE")[0]]


@pytest.mark.parametrize("actor", ["bob", "admin"])
def test_authorized_queryset_budget(access, actor):
    from examples.notes.models import Note
    from skeleton.access.policies import authorized_queryset

    user = getattr(access, actor)
    baseline = app_state_queries(user, access.a)
    captured = decision(user, access.a, lambda: authorized_queryset(Note, user=user))
    assert len(captured) - baseline <= DECISION_BUDGET, [query["sql"] for query in captured]
    assert len(membership_reads(captured)) == 1


def test_require_record_action_budget(access):
    from skeleton.access.policies import require_record_action

    baseline = app_state_queries(access.bob, access.a)
    captured = decision(
        access.bob, access.a, lambda: require_record_action(access.shared, user=access.bob, action="read")
    )
    assert len(captured) - baseline <= DECISION_BUDGET + 1
    assert len(membership_reads(captured)) == 1


def test_audit_queryset_budget_does_not_grow_per_table_without_permission(access):
    from skeleton.access.policies import authorized_audit_queryset
    from skeleton.system.registry import all_specs

    exposed = [spec for spec in all_specs() if spec.expose_api]
    per_audited_table = app_state_queries(access.bob, access.a) + 1  # App state and its TableDefinition.
    captured = decision(access.bob, access.a, lambda: authorized_audit_queryset(access.bob))
    # Membership, the administrator EXISTS and the permitted pairs, plus a fixed cost for each
    # table the actor may audit (Bob audits only Note): never a cost per exposed table.
    assert len(exposed) >= 1
    assert len(captured) <= 3 + per_audited_table, [query["sql"] for query in captured]


def test_revocation_in_the_same_transaction_is_seen(access):
    from examples.notes.models import Note
    from skeleton.access.models import MemberRole
    from skeleton.access.policies import authorized_queryset
    from skeleton.access.services import revoke_link

    with actor_tenant(access.bob, access.a):
        assert authorized_queryset(Note, user=access.bob).exists()
    with actor_tenant(access.admin, access.a):
        for link in MemberRole.objects.filter(membership=access.bob_membership):
            revoke_link(link, user=access.admin)
    with actor_tenant(access.bob, access.a):
        with pytest.raises(PermissionDenied):
            authorized_queryset(Note, user=access.bob)
