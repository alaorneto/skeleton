"""Reminders (#73): exactly once per occurrence, attendees only while invited, cancellations skipped."""

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError

from skeleton.contrib.calendar import attendees, reminders, services
from skeleton.tenancy.context import system_context
from tools.security_fixtures import actor_tenant

NOW = datetime(2026, 12, 3, 8, 0, tzinfo=UTC)


def notifications(agenda):
    from skeleton.notifications.models import Notification

    with actor_tenant(agenda.admin, agenda.a):
        return list(Notification.objects.filter(type_code=reminders.NOTIFICATION).values("dedupe_key", "recipients"))


def run(agenda, now):
    with system_context(reason="test:reminders", organization_id=agenda.a.pk):
        return reminders.send_due_reminders(now=now)


@pytest.fixture
def daily(agenda):
    with actor_tenant(agenda.alice, agenda.a):
        calendar = services.ensure_personal_calendar(agenda.alice)
        start = NOW + timedelta(minutes=30)
        event = services.create_event(
            actor=agenda.alice,
            calendar_id=calendar.pk,
            data={
                "title": "Daily",
                "starts_at": start,
                "ends_at": start + timedelta(minutes=15),
                "rrule": "FREQ=DAILY;COUNT=3",
            },
        )
        reminders.add_reminder(actor=agenda.alice, event_id=event.pk, minutes_before=30)
        attendees.invite(actor=agenda.alice, event_id=event.pk, user_ids=(agenda.bob.pk,))
    return event


def test_each_reminder_fires_once_per_occurrence(agenda, daily):
    assert run(agenda, NOW - timedelta(minutes=10)) == 0  # Not due yet.
    assert run(agenda, NOW) == 1
    assert run(agenda, NOW + timedelta(minutes=5)) == 1  # Re-run inside the grace window: same dedupe key.
    rows = notifications(agenda)
    assert len(rows) == 1 and rows[0]["dedupe_key"].endswith(":30")
    assert run(agenda, NOW + timedelta(days=1)) == 1  # The next occurrence.
    assert len(notifications(agenda)) == 2


def test_removed_attendee_and_cancelled_occurrence(agenda, daily):
    from skeleton.contrib.calendar.models import EventAttendee

    with actor_tenant(agenda.alice, agenda.a):
        invitation = EventAttendee.objects.get(event=daily)
        attendees.remove(actor=agenda.alice, attendee_id=invitation.pk)
        tomorrow = daily.starts_at + timedelta(days=1)
        daily.refresh_from_db()
        services.cancel_occurrence(
            actor=agenda.alice, event_id=daily.pk, recurrence_id=tomorrow, expected_revision=daily.revision
        )
    run(agenda, NOW)
    [row] = notifications(agenda)
    assert row["recipients"]["users"] == [str(agenda.alice.pk)]  # Bob was removed.
    assert run(agenda, NOW + timedelta(days=1)) == 0  # The cancelled occurrence has no reminder.


def test_cancelled_event_and_limits(agenda, daily):
    with actor_tenant(agenda.alice, agenda.a):
        with pytest.raises(ValidationError):
            reminders.add_reminder(actor=agenda.alice, event_id=daily.pk, minutes_before=40321)
        daily.refresh_from_db()
        services.cancel_event(actor=agenda.alice, event_id=daily.pk, expected_revision=daily.revision)
    assert run(agenda, NOW) == 0


def test_the_periodic_job_is_registered_per_organization(agenda, daily):
    from skeleton.jobs.models import Job
    from skeleton.jobs.periodic import schedule_due

    schedule_due(now=NOW)
    with system_context(reason="test:reminders-jobs"):
        organizations = set(
            Job.all_objects.filter(job_type=reminders.JOB).values_list("job_organization_id", flat=True)
        )
    assert {agenda.a.pk, agenda.b.pk} <= organizations


def test_reminder_job_is_not_scheduled_for_an_organization_without_the_app(agenda, daily):
    """With the calendar disabled in B, the reminder job is scheduled for A only (#337)."""
    from skeleton.jobs.models import Job
    from skeleton.jobs.periodic import schedule_due
    from skeleton.system.applications import application_state, configure_application

    with actor_tenant(agenda.bob, agenda.b):
        state = application_state("skeleton_calendar")
        configure_application(
            "skeleton_calendar",
            user=agenda.bob,
            enabled=False,
            configuration=state["configuration"],
            revision=state["revision"],
        )
    schedule_due(now=NOW)
    with system_context(reason="test:reminders-jobs"):
        organizations = set(
            Job.all_objects.filter(job_type=reminders.JOB).values_list("job_organization_id", flat=True)
        )
    assert agenda.a.pk in organizations
    assert agenda.b.pk not in organizations
