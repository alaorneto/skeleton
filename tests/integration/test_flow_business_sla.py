"""SLA of flow tasks in the organization's business hours (#417)."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from examples.notes.models import Note
from skeleton.flows import registry as flows
from skeleton.flows.models import FlowActivity, FlowTask
from skeleton.jobs.models import Job
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import build_service_scenario
from tests.integration.test_flow_service_api import as_app, service

pytestmark = pytest.mark.needs_example_app
KEY = "example_notes.business_sla"
ZONE = "America/Sao_Paulo"


def spec(business_hours=True):
    return flows.FlowSpec(
        key=KEY,
        version=1,
        label="SLA em horas úteis",
        owner_app="example_notes",
        subject_table="example_notes.note",
        steps=(
            flows.start("start"),
            flows.task(
                "work",
                assignees=(flows.Assignment.starter(),),
                sla=flows.Sla(due_seconds=8 * 3600, remind_before_seconds=(3600,), business_hours=business_hours),
            ),
            flows.end("done"),
        ),
        transitions=(flows.Transition("start", "work"), flows.Transition("work", "done", key="finish")),
    )


@pytest.fixture
def world(skeleton_db, monkeypatch):
    from skeleton.flows import registry

    for name in ("_flows", "_handlers", "_conditions", "_assignments"):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(registry, "_duplicates", [])
    flows.register_flow(spec())
    assert flows.flow_problems() == []
    built = build_service_scenario(Note, slug="business-sla")
    built.note = built.create(built.a, code="b1", title="Prazo")
    return built


def open_all_day_except_today(world):
    """Open 24 hours every day, today a holiday: business time starts at tomorrow's midnight."""
    from skeleton.tenancy.models import OrganizationSettings

    today = datetime.now(ZoneInfo(ZONE)).date()
    with system_context(reason="test:business-sla", organization_id=world.a.pk):
        OrganizationSettings(
            organization_id=world.a.pk,
            default_timezone=ZONE,
            business_hours={str(day): [["00:00", "24:00"]] for day in range(7)},
            holidays=[today.isoformat()],
        ).save()
    return datetime.combine(today + timedelta(days=1), time.min, tzinfo=ZoneInfo(ZONE))


def started(world):
    receipt = world.caller(lambda actor: as_app(service.start)(actor=actor, flow_key=KEY, subject_id=world.note.pk))()
    with system_context(reason="test:business-sla", organization_id=world.a.pk):
        task = FlowTask.objects.get(instance_id=receipt.sysid)
        reminders = sorted(
            Job.all_objects.filter(job_type="flows.task_reminder", payload__task_id=str(task.pk)).values_list(
                "run_after", flat=True
            )
        )
        kinds = set(FlowActivity.objects.filter(instance_id=receipt.sysid).values_list("kind", flat=True))
    return task, reminders, kinds


def test_the_deadline_and_the_reminder_follow_the_business_calendar(world):
    opening = open_all_day_except_today(world)
    task, reminders, kinds = started(world)
    assert task.due_at == opening + timedelta(hours=8)
    assert reminders == [opening + timedelta(hours=7)]  # One hour of business time before the deadline.
    assert "sla_elapsed_fallback" not in kinds


def test_without_a_calendar_the_sla_counts_elapsed_time_and_says_so(world):
    task, reminders, kinds = started(world)
    assert timedelta(hours=8) - timedelta(seconds=5) < task.due_at - task.created_at < timedelta(hours=8, seconds=5)
    assert reminders == [task.due_at - timedelta(hours=1)]
    assert "sla_elapsed_fallback" in kinds


def test_flows_without_the_flag_keep_their_canonical_form():
    raw = spec(business_hours=False).to_json()
    assert "business_hours" not in raw["steps"][1]["sla"]
    assert spec().to_json()["steps"][1]["sla"]["business_hours"] is True
    assert flows.FlowSpec.from_json(spec().to_json()) == spec()
