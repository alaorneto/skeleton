"""Shared delivery bookkeeping: creation, recipient checks, retries and links."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

EMAIL_JOB = "notifications.email"
PUSH_JOB = "notifications.push"
# Delay before attempt n+1; after the last one the delivery is dead.
RETRY_SCHEDULE = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
MAX_ATTEMPTS = len(RETRY_SCHEDULE) + 1
OPEN_STATES = ("pending", "retrying")


def job_for(channel: str) -> str:
    return PUSH_JOB if channel == "push" else EMAIL_JOB


def enqueue_attempt(delivery: Any, *, run_after: Any = None) -> None:
    from skeleton.jobs.services import enqueue

    enqueue(
        job_for(delivery.channel),
        organization_id=delivery.organization_id,
        payload={"delivery_id": str(delivery.pk)},
        idempotency_key=f"notification-delivery:{delivery.pk}:{delivery.attempts}",
        run_after=run_after,
    )


def create_delivery(
    *, organization_id: Any, user_id: Any, channel: str, dedupe_key: str, notification_id: Any = None, items=()
) -> Any | None:
    """Create the delivery and its first job; an existing ``dedupe_key`` returns ``None``."""
    from ..models import NotificationDelivery

    if NotificationDelivery.objects.filter(dedupe_key=dedupe_key).exists():
        return None
    delivery = NotificationDelivery(
        organization_id=organization_id,
        notification_id=notification_id,
        user_id=user_id,
        channel=channel,
        dedupe_key=dedupe_key,
        items=[str(item) for item in items],
    )
    try:
        with transaction.atomic():
            delivery.save()
    except IntegrityError:
        return None  # A concurrent run created it first.
    enqueue_attempt(delivery)
    return delivery


def finish(delivery: Any, state: str, **values: Any) -> None:
    delivery.state = state
    delivery.next_attempt_at = None
    for name, value in values.items():
        setattr(delivery, name, value)
    delivery.save(update_fields={"state", "next_attempt_at", *values})


def failed_attempt(delivery: Any, error_code: str, *, retry: bool = True) -> None:
    """Record a failed send; schedule the next attempt or mark the delivery dead."""
    delivery.attempts += 1
    if retry and delivery.attempts < MAX_ATTEMPTS:
        delivery.state = "retrying"
        delivery.error_code = error_code[:64]
        delivery.next_attempt_at = timezone.now() + RETRY_SCHEDULE[delivery.attempts - 1]
        delivery.save(update_fields={"state", "attempts", "error_code", "next_attempt_at"})
        enqueue_attempt(delivery, run_after=delivery.next_attempt_at)
        return
    finish(delivery, "dead", attempts=delivery.attempts, error_code=error_code[:64])


def active_recipient(delivery: Any) -> Any | None:
    """The person, if still an active human member of the delivery's organization."""
    from skeleton.accounts.models import User
    from skeleton.tenancy.models import Membership

    user = User.objects.filter(pk=delivery.user_id, is_active=True, account_type="human").first()
    if user is None:
        return None
    if not Membership.objects.filter(
        user_id=user.pk, organization_id=delivery.organization_id, is_active=True
    ).exists():
        return None
    return user


def target_readable(notification: Any, user: Any) -> bool:
    """Re-authorize the target at send time: lost access skips the delivery."""
    if not notification.target_sysid:
        return True
    from django.core.exceptions import PermissionDenied

    from skeleton.access.policies import require_record_action
    from skeleton.system.registry import get_spec

    try:
        model = get_spec(notification.target_table_key).model
    except KeyError:
        return False
    target = model.objects.filter(pk=notification.target_sysid).first()
    if target is None:
        return False
    try:
        require_record_action(target, user=user, action="read")
    except PermissionDenied:
        return False
    return True


def absolute(path: str) -> str:
    """Links in emails and pushes; ``SKELETON_PUBLIC_URL`` makes them absolute."""
    base = (getattr(settings, "SKELETON_PUBLIC_URL", "") or "").rstrip("/")
    return f"{base}{path}"


def preferences_url() -> str:
    return absolute(reverse("notifications:preferences"))


def open_url(notification: Any, user_id: Any) -> str:
    from ..models import NotificationRecipient

    row = NotificationRecipient.objects.filter(notification_id=notification.pk, user_id=user_id).first()
    if row is None:
        return absolute(reverse("notifications:organization", kwargs={"organization_id": notification.organization_id}))
    return absolute(
        reverse("notifications:open", kwargs={"organization_id": notification.organization_id, "sysid": row.pk})
    )
