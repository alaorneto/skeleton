"""Absence substitution: tasks of someone away go to their substitute for the period (#418)."""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from skeleton.flows.models import FlowActivity, FlowTask
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD
from tests import support
from tests.integration.test_flow_service_api import scenario, start

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]


def client(user):
    return support.login(user, password=FIXTURE_PASSWORD, csrf=False)


def record(http, organization, user, substitute, *, starts=-1, ends=24):
    now = timezone.now()
    body = {
        "user": str(user.pk),
        "substitute": str(substitute.pk),
        "starts_at": (now + timedelta(hours=starts)).isoformat(),
        "ends_at": (now + timedelta(hours=ends)).isoformat(),
    }
    return http.post(
        reverse("flows:api-absences", args=[organization.pk]), data=json.dumps(body), content_type="application/json"
    )


def fill_task(scenario, receipt):
    with system_context(reason="test:absences", organization_id=scenario.a.pk):
        task = FlowTask.objects.get(instance_id=receipt.sysid, step_key="fill")
        kinds = set(FlowActivity.objects.filter(instance_id=receipt.sysid).values_list("kind", flat=True))
    return task, kinds


def test_a_task_of_someone_away_goes_to_the_substitute(scenario):
    member = client(scenario.member)
    created = record(member, scenario.a, scenario.member, scenario.admin_a)
    assert created.status_code == 201, created.content
    task, kinds = fill_task(scenario, start(scenario))  # "fill" is assigned to the starter, who is away.
    assert (task.assignee_id, task.delegated_from_id) == (scenario.admin_a.pk, scenario.member.pk)
    assert "absence_delegated" in kinds
    listed = member.get(reverse("flows:api-absences", args=[scenario.a.pk])).json()["results"]
    assert [item["substitute_id"] for item in listed] == [str(scenario.admin_a.pk)]


def test_outside_the_period_or_without_reading_the_record_nothing_moves(scenario):
    member = client(scenario.member)
    record(member, scenario.a, scenario.member, scenario.admin_a, starts=48, ends=72)  # Not yet.
    task, _ = fill_task(scenario, start(scenario))
    assert (task.assignee_id, task.delegated_from_id) == (scenario.member.pk, None)
    record(member, scenario.a, scenario.member, scenario.denied)  # Now, but the substitute cannot read notes.
    task, kinds = fill_task(scenario, start(scenario, note=scenario.other))
    assert task.assignee_id == scenario.member.pk and "absence_substitute_refused" in kinds


def test_who_may_record_and_remove_an_absence(scenario):
    member = client(scenario.member)
    assert record(member, scenario.a, scenario.denied, scenario.admin_a).status_code == 403
    assert record(member, scenario.a, scenario.member, scenario.admin_b).status_code == 400  # Not a member of A.
    assert record(member, scenario.a, scenario.member, scenario.member).status_code == 400
    admin = client(scenario.admin_a)
    made = record(admin, scenario.a, scenario.denied, scenario.member)
    assert made.status_code == 201
    absence = made.json()
    url = reverse("flows:api-absence", args=[scenario.a.pk, absence["sysid"]])
    assert member.delete(url, HTTP_IF_MATCH=f'"{absence["revision"]}"').status_code == 404  # Not the member's.
    assert admin.delete(url, HTTP_IF_MATCH=f'"{absence["revision"] + 1}"').status_code == 412
    assert admin.delete(url, HTTP_IF_MATCH=f'"{absence["revision"]}"').status_code == 204
    assert admin.get(reverse("flows:api-absences", args=[scenario.a.pk])).json()["results"] == []
