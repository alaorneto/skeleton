"""Operations panel of the flows (#408): who sees it, what it counts, and nothing of the forms."""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from skeleton.flows.operations import overview
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD
from tests import support
from tests.integration.test_flow_process_manager import grant
from tests.integration.test_flow_service_api import SECRET, fill, my_task, scenario, start

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]


def panel(scenario, user=None, organization=None, **filters):
    with scenario.acting(user or scenario.admin_a, organization or scenario.a) as actor:
        return overview(actor, **filters)


def prepare(scenario):
    """One failed instance, one queue task without an assignee and one overdue task, in A."""
    from skeleton.flows.models import FlowInstance, FlowTask

    failing = start(scenario)
    with system_context(reason="test:ops-failed", organization_id=scenario.a.pk):
        FlowInstance.objects.filter(pk=failing.sysid).update(status="failed")
    queued = start(scenario, note=scenario.other)
    fill(scenario, remark=SECRET)  # Completes one fill task: its review becomes a queue task without assignee.
    late = my_task(scenario)
    with system_context(reason="test:ops-overdue", organization_id=scenario.a.pk):
        FlowTask.objects.filter(pk=late.sysid).update(due_at=timezone.now() - timedelta(hours=2))
    return failing, queued, late


def test_the_panel_counts_and_lists_what_needs_action(scenario):
    failing, _, late = prepare(scenario)
    seen = panel(scenario)
    assert seen.counts.failed == 1 and [row.sysid for row in seen.failed] == [failing.sysid]
    assert seen.counts.unassigned == 1 and seen.unassigned[0].assignee_id is None
    assert seen.unassigned[0].step_key == "review"
    assert seen.counts.overdue == 1 and seen.overdue[0].sysid == late.sysid
    assert seen.counts.due_soon == 0
    assert seen.flow_keys == ("example_notes.approval",)
    assert panel(scenario, flow_key="other.flow").counts.failed == 0
    assert panel(scenario, flow_version=2).counts.overdue == 0


def test_only_who_administers_flows_sees_it_and_only_its_organization(scenario):
    prepare(scenario)
    theirs = start(scenario, note=scenario.foreign, organization=scenario.b)
    with pytest.raises(PermissionDenied):
        panel(scenario, user=scenario.member)
    grant(scenario, scenario.denied)
    managed = panel(scenario, user=scenario.denied)
    assert managed.counts.failed == 1
    assert theirs.sysid not in {row.sysid for row in managed.failed}
    assert panel(scenario, user=scenario.admin_b, organization=scenario.b).counts.failed == 0


def test_the_number_of_queries_does_not_grow_with_the_instances(scenario):
    prepare(scenario)
    with scenario.acting(scenario.admin_a) as actor:
        with CaptureQueriesContext(connection) as few:
            overview(actor)
    for index in range(4):
        note = scenario.create(scenario.a, code=f"more-{index}", title=f"More {index}")
        start(scenario, note=note)
    with scenario.acting(scenario.admin_a) as actor:
        with CaptureQueriesContext(connection) as many:
            overview(actor)
    assert len(many.captured_queries) == len(few.captured_queries)


def test_page_and_rest_answer_the_administrator_and_refuse_the_member(scenario):
    prepare(scenario)
    admin = support.login(scenario.admin_a, password=FIXTURE_PASSWORD, csrf=False)
    page = admin.get(reverse("flows:operations", args=[scenario.a.pk]))
    assert page.status_code == 200
    body = page.content.decode()
    assert 'data-count="failed">1<' in body and SECRET not in body and "Resumo" not in body
    api = admin.get(reverse("flows:api-operations", args=[scenario.a.pk]), {"flow_key": "example_notes.approval"})
    assert api.status_code == 200, api.content
    data = api.json()
    assert data["counts"] == {"failed": 1, "unassigned": 1, "overdue": 1, "due_soon": 0}
    assert SECRET not in api.content.decode()
    assert admin.get(reverse("flows:api-operations", args=[scenario.a.pk]), {"version": "x"}).status_code == 400
    member = support.login(scenario.member, password=FIXTURE_PASSWORD, csrf=False)
    assert member.get(reverse("flows:operations", args=[scenario.a.pk])).status_code == 403
    assert member.get(reverse("flows:api-operations", args=[scenario.a.pk])).status_code == 403
