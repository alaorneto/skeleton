"""Retention of notification rows (#57): the only path that physically deletes them.

The periodic job ``notifications.retention`` runs daily per organization, in that
organization's system context. It removes, in this order (rows referenced with PROTECT go
last):

1. deliveries older than the delivery window: finished ones (``sent``, ``skipped``,
   ``dead``) and abandoned open ones (``pending``, ``retrying``) whose job died, since the
   retry schedule lasts hours, not weeks (#277);
2. inbox rows read or archived longer than the read window, and unread rows whose last
   update is older than the unread window;
3. notifications older than the unread window that no inbox row or delivery references
   anymore, fanned out or not (a fan-out job that died leaves ``fanned_out_at`` empty).

Open deliveries **within** the window and live rows are never touched, so the unread
counter keeps counting exactly the remaining unread rows.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.utils import timezone

RETENTION_JOB = "notifications.retention"
DEFAULT_READ_DAYS = 90
DEFAULT_UNREAD_DAYS = 180
DEFAULT_DELIVERY_DAYS = 30
FINAL_DELIVERY_STATES = ("sent", "skipped", "dead")
OPEN_DELIVERY_STATES = ("pending", "retrying")
BATCH = 1000


def windows() -> dict[str, timedelta]:
    return {
        "read": timedelta(days=int(getattr(settings, "SKELETON_NOTIFICATION_READ_RETENTION_DAYS", DEFAULT_READ_DAYS))),
        "unread": timedelta(
            days=int(getattr(settings, "SKELETON_NOTIFICATION_UNREAD_RETENTION_DAYS", DEFAULT_UNREAD_DAYS))
        ),
        "deliveries": timedelta(
            days=int(getattr(settings, "SKELETON_NOTIFICATION_DELIVERY_RETENTION_DAYS", DEFAULT_DELIVERY_DAYS))
        ),
    }


def expired(now: Any = None) -> dict[str, Any]:
    """The querysets the job deletes, in order; exposed so tests can inspect the selection."""
    from django.db.models import Exists, OuterRef, Q

    from .models import Notification, NotificationDelivery, NotificationRecipient

    now = now or timezone.now()
    limits = windows()
    read_before, unread_before = now - limits["read"], now - limits["unread"]
    deliveries = NotificationDelivery.objects.filter(
        state__in=(*FINAL_DELIVERY_STATES, *OPEN_DELIVERY_STATES), created_at__lt=now - limits["deliveries"]
    )
    rows = NotificationRecipient.objects.filter(
        Q(read_at__lt=read_before)
        | Q(archived_at__lt=read_before)
        | Q(read_at__isnull=True, archived_at__isnull=True, updated_at__lt=unread_before)
    )
    notifications = Notification.objects.filter(created_at__lt=unread_before).exclude(
        Exists(NotificationRecipient.objects.filter(notification_id=OuterRef("pk")))
        | Exists(NotificationDelivery.objects.filter(notification_id=OuterRef("pk")))
    )
    return {"deliveries": deliveries, "rows": rows, "notifications": notifications}


def _delete(queryset: Any) -> int:
    total = 0
    while True:
        ids = list(queryset.order_by("pk").values_list("pk", flat=True)[:BATCH])
        if not ids:
            return total
        count, _ = queryset.model.objects.filter(pk__in=ids).delete()
        total += count


def purge_expired_notifications(*, now: Any = None) -> dict[str, int]:
    """Job handler (periodic, system context of one organization)."""
    selection = expired(now)
    result = {"deliveries": _delete(selection["deliveries"]), "rows": _delete(selection["rows"])}
    # Re-evaluated after the deletes above: they free notifications from their PROTECT references.
    result["notifications"] = _delete(expired(now)["notifications"])
    return result
