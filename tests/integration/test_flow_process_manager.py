"""The builtin "Gestor de processos" administers flows through ``flows.manage`` (#407).

The role goes to ``denied``, the member of A without any table permission: whatever it can do in
flows comes from the capability, and nothing outside flows.
"""

import pytest

from skeleton.access.capabilities import BUILTIN_ROLES, FLOWS_MANAGE, has_capability
from skeleton.service_api import Forbidden, NotFound
from skeleton.tenancy.context import tenant_context
from tests.integration.test_flow_service_api import call, my_task, scenario, service, start

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]  # The fixture, imported for this module's tests.


def grant(scenario, user, organization=None):
    from skeleton.access.models import MemberRole, Role
    from skeleton.tenancy.models import Membership

    organization = organization or scenario.a
    admin = scenario.admin_a if organization.pk == scenario.a.pk else scenario.admin_b
    with tenant_context(organization_id=organization.pk, actor=admin, source="test:process-manager"):
        membership = Membership.objects.get(user=user, is_active=True)
        role = Role.objects.get(code="process_manager", is_builtin=True)
        return MemberRole.objects.create(organization=organization, membership=membership, role=role)


def revoke(scenario, assignment):
    with tenant_context(organization_id=scenario.a.pk, actor=scenario.admin_a, source="test:process-manager"):
        assignment.delete()


def test_the_role_carries_only_flows_manage():
    assert BUILTIN_ROLES["process_manager"].capabilities == {FLOWS_MANAGE}
    assert FLOWS_MANAGE in BUILTIN_ROLES["organization_admin"].capabilities


def test_the_process_manager_operates_every_instance_and_loses_it_on_revocation(scenario):
    receipt = start(scenario)
    manager = scenario.denied
    with pytest.raises((Forbidden, NotFound)):
        call(
            scenario,
            lambda actor: service.suspend(actor=actor, instance_id=receipt.sysid, expected_revision=receipt.revision),
            user=manager,
        )
    assignment = grant(scenario, manager)
    with scenario.acting(manager):
        assert has_capability(manager, FLOWS_MANAGE)
        assert not has_capability(manager, "members.manage")
    info = call(scenario, lambda actor: service.instance(actor=actor, instance_id=receipt.sysid), user=manager)
    assert receipt.sysid in {
        item.sysid for item in call(scenario, lambda actor: service.instances(actor=actor).items, user=manager)
    }
    suspended = call(
        scenario,
        lambda actor: service.suspend(actor=actor, instance_id=receipt.sysid, expected_revision=info.revision),
        user=manager,
    )
    assert suspended.status == "suspended"
    resumed = call(
        scenario,
        lambda actor: service.resume(actor=actor, instance_id=receipt.sysid, expected_revision=suspended.revision),
        user=manager,
    )
    assert resumed.status == "running"
    task = my_task(scenario)  # The starter's open task.
    seen = call(scenario, lambda actor: service.task(actor=actor, task_id=task.sysid), user=manager)
    assert seen.can_reassign
    call(
        scenario,
        lambda actor: service.reassign_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, to_user_id=scenario.admin_a.pk
        ),
        user=manager,
    )
    revoke(scenario, assignment)
    with pytest.raises((Forbidden, NotFound)):
        call(
            scenario,
            lambda actor: service.cancel(actor=actor, instance_id=receipt.sysid, expected_revision=resumed.revision),
            user=manager,
        )


def test_the_capability_is_bound_to_its_organization(scenario):
    theirs = start(scenario, note=scenario.foreign, organization=scenario.b)
    grant(scenario, scenario.denied)
    with pytest.raises(NotFound):
        call(scenario, lambda actor: service.instance(actor=actor, instance_id=theirs.sysid), user=scenario.denied)
    assert theirs.sysid not in {
        item.sysid for item in call(scenario, lambda actor: service.instances(actor=actor).items, user=scenario.denied)
    }
