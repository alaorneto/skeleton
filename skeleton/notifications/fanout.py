"""Fan-out job: turn a notification into one row per person."""

from __future__ import annotations

from django.utils import timezone


def _people(notification) -> list:
    from skeleton.tenancy.models import Membership

    spec = notification.recipients
    members = Membership.objects.filter(
        is_active=True, user__is_active=True, user__deleted_at__isnull=True
    ).values_list("user_id", flat=True)
    if spec.get("organization"):
        people = set(members)
    else:
        people = set(members.filter(user_id__in=spec.get("users", ())))
    people.discard(notification.actor_id)
    return sorted(people, key=str)


def fan_out(*, notification_id: str, offset: int) -> int:
    """Job handler (system context of the organization)."""
    from .models import Notification, NotificationRecipient

    notification = Notification.objects.filter(pk=notification_id).first()
    if notification is None or notification.fanned_out_at is not None:
        return 0
    rows = [
        NotificationRecipient(organization_id=notification.organization_id, notification=notification, user_id=user)
        for user in _people(notification)
    ]
    NotificationRecipient.objects.bulk_create(rows)
    notification.fanned_out_at = timezone.now()
    notification.save(update_fields={"fanned_out_at"})
    return len(rows)
