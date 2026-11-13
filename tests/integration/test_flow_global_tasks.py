"""Open tasks of a person across organizations and the header counter (#409)."""

import pytest
from django.urls import reverse

from skeleton.tenancy.context import tenant_context
from skeleton.testing.service_api import FIXTURE_PASSWORD
from tests import support
from tests.integration.test_flow_service_api import my_task, scenario, start

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]


def both(scenario):
    """One open task assigned to the member in A and one in B (the "fill" step of each start)."""
    start(scenario)
    start(scenario, note=scenario.foreign, organization=scenario.b)
    return my_task(scenario), my_task(scenario, organization=scenario.b)


def test_the_person_sees_their_own_tasks_of_every_organization_and_the_counter(scenario):
    in_a, in_b = both(scenario)
    client = support.login(scenario.member, password=FIXTURE_PASSWORD, csrf=False)
    page = client.get(reverse("flows:global-tasks"))
    assert page.status_code == 200
    body = page.content.decode()
    assert str(in_a.sysid) in body and str(in_b.sysid) in body
    assert scenario.a.name in body and scenario.b.name in body
    assert "data-flow-tasks>2<" in body  # The header counter, on any page.
    assert "data-flow-tasks>2<" in client.get(reverse("notifications:global")).content.decode()


def test_other_people_and_inactive_memberships_see_nothing(scenario):
    from skeleton.tenancy.models import Membership

    in_a, in_b = both(scenario)
    other = support.login(scenario.denied, password=FIXTURE_PASSWORD, csrf=False)
    body = other.get(reverse("flows:global-tasks")).content.decode()
    assert str(in_a.sysid) not in body and str(in_b.sysid) not in body and "data-flow-tasks" not in body
    with tenant_context(organization_id=scenario.b.pk, actor=scenario.admin_b, source="test:global-tasks"):
        membership = Membership.objects.get(user=scenario.member)
        membership.is_active = False
        membership.save(update_fields=["is_active"])
    member = support.login(scenario.member, password=FIXTURE_PASSWORD, csrf=False)
    body = member.get(reverse("flows:global-tasks")).content.decode()
    assert str(in_a.sysid) in body and str(in_b.sysid) not in body
    assert "data-flow-tasks>1<" in body
    assert (
        support.login(scenario.outsider, password=FIXTURE_PASSWORD, csrf=False)
        .get(reverse("flows:global-tasks"))
        .status_code
        == 200
    )


def test_the_runtime_role_reads_its_own_tasks_of_another_tenant_only_through_the_assignee():
    """The frozen specification carries the column; the deployed policies are checked by the schema check."""
    from skeleton.flows.models import FlowTask

    assert FlowTask.self_discovery_field == "assignee"
