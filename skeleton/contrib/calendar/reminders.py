"""Event reminders (#73): a periodic job per organization, every 5 minutes.

For each reminder, the job expands the occurrences that start within ``minutes_before`` plus a
grace window, and notifies the organizer and the active attendees of each occurrence whose
reminder time has come. The dedupe key ``reminder:<event>:<occurrence>:<minutes>`` makes every
reminder fire exactly once per occurrence, however often the job runs. Cancelled events and
cancelled or excluded occurrences produce nothing; the final channel follows the person's
notification preferences.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext

from .models import EventReminder

JOB = "calendar.reminders"
NOTIFICATION = "calendar.reminder"
EVERY = timedelta(minutes=5)
GRACE = timedelta(minutes=30)


def add_reminder(*, actor: Any, event_id: Any, minutes_before: int) -> EventReminder:
    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    if type(minutes_before) is not int or not 0 <= minutes_before <= 40320:
        raise ValidationError({"minutes_before": gettext("Use de 0 a 40320 minutos (28 dias).")})
    reminder = EventReminder.objects.filter(event=event, minutes_before=minutes_before).first()
    if reminder is None:
        reminder = EventReminder(organization_id=event.organization_id, event=event, minutes_before=minutes_before)
        reminder.save()
    return reminder


def remove_reminder(*, actor: Any, reminder_id: Any) -> None:
    from .policies import require_writable_event

    reminder = EventReminder.objects.filter(pk=reminder_id).first()
    if reminder is None:
        raise ValidationError({"reminder": gettext("Lembrete indisponível.")})
    require_writable_event(actor, reminder.event_id)
    reminder.delete()


def _recipients(event: Any) -> tuple:
    from .attendees import attendee_users

    return tuple(dict.fromkeys((event.organizer_id, *attendee_users(event))))


def send_due_reminders(*, now: Any = None) -> int:
    """Job handler (system context of the organization): notify the reminders that are due."""
    from skeleton.service_api.notifications import NotificationService, Recipients

    from .models import Event
    from .recurrence import expand

    now = now or timezone.now()
    sent = 0
    reminders = EventReminder.objects.select_related("event").filter(
        event__status__in=(Event.Status.CONFIRMED, Event.Status.TENTATIVE), event__series__isnull=True
    )
    for reminder in reminders:
        event, lead = reminder.event, timedelta(minutes=reminder.minutes_before)
        overrides = list(Event.objects.filter(series=event))
        window = expand(event, now - GRACE + lead, now + lead + EVERY, overrides=overrides)
        for occurrence in window.occurrences:
            due = occurrence.starts_at - lead
            if not (now - GRACE < due <= now) or occurrence.event.status == Event.Status.CANCELLED:
                continue
            key = (occurrence.recurrence_id or occurrence.starts_at).strftime("%Y%m%dT%H%M%SZ")
            NotificationService().notify(
                actor=None,
                type_code=NOTIFICATION,
                recipients=Recipients(users=_recipients(event)),
                payload=(
                    ("title", occurrence.event.title if not occurrence.event.is_private else "Ocupado"),
                    ("starts_at", occurrence.starts_at.isoformat()),
                    ("event", str(event.pk)),
                ),
                dedupe_key=f"reminder:{event.pk}:{key}:{reminder.minutes_before}",
            )
            sent += 1
    return sent


def register_reminders() -> None:
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type
    from skeleton.notifications.registry import NotificationType, register_notification_type

    register_notification_type(
        NotificationType(
            NOTIFICATION,
            "Lembrete de evento",
            payload_fields=("title", "starts_at", "event"),
            default_channels=("in_app", "email"),
            owner_app="skeleton_calendar",
        )
    )
    register_job_type(JobType(JOB, send_due_reminders, {}, owner_app="skeleton_calendar", max_attempts=3))
    register_periodic(JOB, JOB, every=EVERY, per_organization=True)
