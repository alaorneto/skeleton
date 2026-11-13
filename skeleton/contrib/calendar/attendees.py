"""Attendees and RSVP (#72): members of the same organization only, RSVP only for oneself."""

from __future__ import annotations

import hashlib
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext

from .models import EventAttendee

INVITED = "calendar.invited"
UPDATED = "calendar.updated"
CANCELLED = "calendar.cancelled"


def _member(actor: Any, user_id: Any) -> Any:
    """An active human member of the current organization, through the core service API (#343)."""
    from skeleton.service_api import InvalidInput
    from skeleton.service_api.organizations import OrganizationService

    try:
        refs = OrganizationService().member_refs(actor=actor, user_ids=(user_id,))
    except InvalidInput:
        refs = ()
    if not refs:
        raise ValidationError({"user": gettext("Convide só pessoas ativas desta organização (não contas de serviço).")})
    return refs[0]


def active_user_ids(user_ids: Any) -> set:
    """Users among ``user_ids`` with an active membership in the current organization (jobs, #223)."""
    from skeleton.service_api.organizations import OrganizationService

    identifiers = tuple(dict.fromkeys(user_ids))
    if not identifiers:
        return set()
    refs = OrganizationService().member_refs(actor=None, user_ids=identifiers, include_service_accounts=True)
    return {ref.user_id for ref in refs}


def invite(*, actor: Any, event_id: Any, user_ids: tuple, optional: bool = False) -> list[EventAttendee]:
    """Invite members to an event the actor may write; existing invitations are kept."""
    from .policies import require_writable_event

    event = require_writable_event(actor, event_id)
    created = []
    invited = []
    for user_id in dict.fromkeys(user_ids):
        ref = _member(actor, user_id)
        if EventAttendee.objects.filter(event=event, membership_id=ref.membership_id).exists():
            continue
        attendee = EventAttendee(
            organization_id=event.organization_id, event=event, membership_id=ref.membership_id, optional=optional
        )
        attendee.save()
        created.append(attendee)
        invited.append(ref.user_id)
    notify_attendees(event, INVITED, users=tuple(invited), actor=actor)
    return created


def remove(*, actor: Any, attendee_id: Any, expected_revision: int) -> None:
    """Remove an invitation, with CAS; the person stops reading the event on the next request."""
    from skeleton.service_api import Conflict

    from .policies import require_writable_event

    attendee = EventAttendee.objects.filter(pk=attendee_id).first()
    if attendee is None:
        raise PermissionDenied(gettext("Participante indisponível."))
    require_writable_event(actor, attendee.event_id)
    if type(expected_revision) is not int or attendee.revision != expected_revision:
        raise Conflict("stale_revision", gettext("O convite mudou."))  # #344
    attendee.delete()


def respond(*, actor: Any, event_id: Any, response: str, expected_revision: int) -> EventAttendee:
    """RSVP of the actor, with CAS; someone else's invitation, or none, is missing (404)."""
    from skeleton.service_api import Conflict

    if response not in EventAttendee.Response.values:
        raise ValidationError({"response": gettext("Use accepted, declined, tentative ou needs_action.")})
    attendee = EventAttendee.objects.filter(
        event_id=event_id, membership__user_id=actor.pk, membership__is_active=True
    ).first()
    if attendee is None:
        raise PermissionDenied(gettext("Convite indisponível."))
    if type(expected_revision) is not int or attendee.revision != expected_revision:
        raise Conflict("stale_revision", gettext("O convite mudou."))  # #344
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
    title = event.title if not event.is_private else gettext("Ocupado")
    key = f"{type_code}:{event.pk}:{event.sequence}"
    if users is not None:
        # An invitation is addressed to specific people: two batches must not share a key (#235).
        # Only a digest of the UUIDs enters the key, never a name or an email.
        digest = hashlib.sha256(",".join(sorted(str(user) for user in targets)).encode()).hexdigest()[:16]
        key = f"{key}:{digest}"
    NotificationService().notify(
        actor=actor,
        type_code=type_code,
        recipients=Recipients(users=targets),
        payload=(("title", title), ("event", str(event.series_id or event.pk))),
        dedupe_key=key,
    )


def register_attendee_notifications() -> None:
    from skeleton.notifications.registry import NotificationType, register_notification_type

    for code, label in (
        (INVITED, gettext("Convite para evento")),
        (UPDATED, gettext("Evento alterado")),
        (CANCELLED, gettext("Evento cancelado")),
    ):
        register_notification_type(
            NotificationType(code, label, payload_fields=("title", "event"), owner_app="skeleton_calendar")
        )
