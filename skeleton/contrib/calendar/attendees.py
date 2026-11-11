"""Attendees and RSVP (#72): members of the same organization only, RSVP only for oneself."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError

from .models import EventAttendee

INVITED = "calendar.invited"
UPDATED = "calendar.updated"
CANCELLED = "calendar.cancelled"


def _membership(user_id: Any) -> Any:
    """An active human member of the current organization (RLS keeps other organizations out)."""
    from django.apps import apps

    membership = (
        apps.get_model("sk_tenancy", "Membership")
        .objects.filter(
            user_id=user_id,
            is_active=True,
            deleted_at__isnull=True,
            user__is_active=True,
            user__account_type="human",
        )
        .first()
    )
    if membership is None:
        raise ValidationError({"user": "Convide só pessoas ativas desta organização (não contas de serviço)."})
    return membership


def invite(*, actor: Any, event_id: Any, user_ids: tuple, optional: bool = False) -> list[EventAttendee]:
    """Invite members to an event the actor may write; existing invitations are kept."""
    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    created = []
    for user_id in dict.fromkeys(user_ids):
        membership = _membership(user_id)
        if EventAttendee.objects.filter(event=event, membership=membership).exists():
            continue
        attendee = EventAttendee(
            organization_id=event.organization_id, event=event, membership=membership, optional=optional
        )
        attendee.save()
        created.append(attendee)
    notify_attendees(event, INVITED, users=tuple(row.membership.user_id for row in created), actor=actor)
    return created


def remove(*, actor: Any, attendee_id: Any) -> None:
    """Remove an invitation; the person stops reading the event on the next request."""
    from .policies import require_writable_event

    attendee = EventAttendee.objects.filter(pk=attendee_id).first()
    if attendee is None:
        raise PermissionDenied("Attendee unavailable.")
    require_writable_event(actor, attendee.event_id)
    attendee.delete()


def respond(*, actor: Any, event_id: Any, response: str) -> EventAttendee:
    """RSVP of the actor; someone else's invitation, or none, is missing (404)."""
    if response not in EventAttendee.Response.values:
        raise ValidationError({"response": "Use accepted, declined, tentative ou needs_action."})
    attendee = EventAttendee.objects.filter(
        event_id=event_id, membership__user_id=actor.pk, membership__is_active=True
    ).first()
    if attendee is None:
        raise PermissionDenied("Invitation unavailable.")
    if attendee.response != response:
        attendee.response = response
        attendee.save(update_fields={"response"})
    return attendee


def attendee_users(event: Any) -> tuple:
    return tuple(
        EventAttendee.objects.filter(event_id=event.series_id or event.pk, membership__is_active=True).values_list(
            "membership__user_id", flat=True
        )
    )


def notify_attendees(event: Any, type_code: str, *, users: tuple | None = None, actor: Any) -> None:
    """Tell the attendees, in the caller's transaction; the payload never carries private content."""
    from skeleton.service_api.notifications import NotificationService, Recipients

    targets = tuple(user for user in (attendee_users(event) if users is None else users) if user != actor.pk)
    if not targets:
        return
    title = event.title if not event.is_private else "Ocupado"
    NotificationService().notify(
        actor=actor,
        type_code=type_code,
        recipients=Recipients(users=targets),
        payload=(("title", title), ("event", str(event.series_id or event.pk))),
        dedupe_key=f"{type_code}:{event.pk}:{event.sequence}",
    )


def register_attendee_notifications() -> None:
    from skeleton.notifications.registry import NotificationType, register_notification_type

    for code, label in (
        (INVITED, "Convite para evento"),
        (UPDATED, "Evento alterado"),
        (CANCELLED, "Evento cancelado"),
    ):
        register_notification_type(
            NotificationType(code, label, payload_fields=("title", "event"), owner_app="skeleton_calendar")
        )
