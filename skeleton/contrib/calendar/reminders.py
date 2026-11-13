"""Event reminders (#73): a periodic job per organization, every 5 minutes.

For each reminder, the job expands the occurrences that start within ``minutes_before`` plus a
grace window, and notifies the organizer and the active attendees of each occurrence whose
reminder time has come. The dedupe key ``reminder:<event>:<occurrence>:<minutes>`` makes every
reminder fire exactly once per occurrence, however often the job runs. Cancelled events and
cancelled or excluded occurrences produce nothing; the final channel follows the person's
notification preferences.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext

from .models import EventReminder

logger = logging.getLogger("skeleton.contrib.calendar")
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


def _recipients(event: Any, invited: dict, organizers: set) -> tuple:
    """The organizer while still a member, then the active attendees (#223)."""
    users = list(invited.get(event.pk, ()))
    if event.organizer_id in organizers:
        users.insert(0, event.organizer_id)
    return tuple(dict.fromkeys(users))


def _candidates(now: Any) -> tuple[list, dict, dict, set]:
    """Reminders that may still be due, with their overrides, attendees and active organizers, in bulk."""
    from django.db.models import Q

    from .attendees import active_user_ids
    from .models import Event, EventAttendee

    cutoff = now - GRACE
    # A series whose last occurrence already ended has no reminder due. A master with an override
    # ending after the cutoff stays a candidate: the override may have been moved later.
    moved = (
        Event.objects.filter(series__isnull=False)
        .filter(Q(ends_at__gt=cutoff) | Q(end_date__gte=cutoff.date()))
        .values("series_id")
    )
    reminders = list(
        EventReminder.objects.select_related("event")
        .filter(
            event__status__in=(Event.Status.CONFIRMED, Event.Status.TENTATIVE),
            event__series__isnull=True,
            event__deleted_at__isnull=True,  # The relation does not hide a deleted event (#292).
        )
        .filter(Q(event__recurrence_end__gt=cutoff) | Q(event__recurrence_end__isnull=True) | Q(event_id__in=moved))
    )
    master_ids = {reminder.event_id for reminder in reminders}
    overrides: dict = {}
    invited: dict = {}
    if master_ids:
        for row in Event.objects.filter(series_id__in=master_ids):
            overrides.setdefault(row.series_id, []).append(row)
        attendance = EventAttendee.objects.filter(event_id__in=master_ids, membership__is_active=True)
        for event_id, user_id in attendance.values_list("event_id", "membership__user_id"):
            invited.setdefault(event_id, []).append(user_id)
    organizers = active_user_ids({reminder.event.organizer_id for reminder in reminders})
    return reminders, overrides, invited, organizers


def send_due_reminders(*, now: Any = None) -> int:
    """Job handler (system context of the organization): notify the reminders that are due.

    Does nothing while the calendar is off in the organization (#223). The organizer is notified
    only while still a member; an occurrence nobody active can receive is skipped, and a
    notification refused as invalid input is logged without rolling the others back.
    """
    from django.core.exceptions import PermissionDenied
    from django.db import transaction

    from skeleton.service_api import InvalidInput
    from skeleton.service_api.notifications import NotificationService, Recipients
    from skeleton.system.applications import require_application_enabled
    from skeleton.tenancy.context import current_context

    from .models import Event
    from .recurrence import expand

    now = now or timezone.now()
    try:
        require_application_enabled("skeleton_calendar")
    except PermissionDenied:  # ApplicationUnavailable too: a job of a closed app does nothing.
        return 0
    sent = 0
    reminders, overrides, invited, organizers = _candidates(now)
    for reminder in reminders:
        event, lead = reminder.event, timedelta(minutes=reminder.minutes_before)
        # Local midnight of an all-day event is up to 14 hours from UTC midnight: read a day more each side.
        span = timedelta(days=1) if event.all_day else timedelta()
        window = expand(
            event, now - GRACE + lead - span, now + lead + EVERY + span, overrides=overrides.get(event.pk, [])
        )
        for occurrence in window.occurrences:
            starts = occurrence.starts_at
            if occurrence.event.all_day:
                # An all-day event starts at midnight in its own zone (#312).
                starts = datetime.combine(starts.date(), time(), tzinfo=ZoneInfo(occurrence.event.tzid))
            due = starts - lead
            if not (now - GRACE < due <= now) or occurrence.event.status == Event.Status.CANCELLED:
                continue
            users = _recipients(event, invited, organizers)
            if not users:
                continue
            key = (occurrence.recurrence_id or occurrence.starts_at).strftime("%Y%m%dT%H%M%SZ")
            try:
                with transaction.atomic(using=current_context().using):
                    NotificationService().notify(
                        actor=None,
                        type_code=NOTIFICATION,
                        recipients=Recipients(users=users),
                        payload=(
                            (
                                "title",
                                occurrence.event.title if not occurrence.event.is_private else gettext("Ocupado"),
                            ),
                            ("starts_at", starts.isoformat()),
                            ("event", str(event.pk)),
                        ),
                        dedupe_key=f"reminder:{event.pk}:{key}:{reminder.minutes_before}",
                    )
            except InvalidInput:
                # Identifiers only: never the title, description or location.
                logger.warning(
                    "calendar.reminder_skipped",
                    extra={"event": "calendar.reminder_skipped", "target_sysid": str(event.pk)},
                )
                continue
            sent += 1
    return sent


def register_reminders() -> None:
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type
    from skeleton.notifications.registry import NotificationType, register_notification_type

    register_notification_type(
        NotificationType(
            NOTIFICATION,
            gettext("Lembrete de evento"),
            payload_fields=("title", "starts_at", "event"),
            default_channels=("in_app", "email"),
            owner_app="skeleton_calendar",
        )
    )
    register_job_type(JobType(JOB, send_due_reminders, {}, owner_app="skeleton_calendar", max_attempts=3))
    register_periodic(JOB, JOB, every=EVERY, per_organization=True)
