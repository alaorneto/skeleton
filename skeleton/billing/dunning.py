"""Dunning (#399): retries, reminders and the progressive access level of an organization.

The access level is **derived**, never typed in: from the oldest unpaid invoice, the
thresholds of the instance and a platform extension.

| Oldest unpaid invoice | Level |
|---|---|
| none overdue | ``active`` |
| overdue for less than ``SKELETON_BILLING_GRACE_DAYS`` full days | ``past_due`` |
| less than ``SKELETON_BILLING_SUSPEND_AFTER_DAYS`` | ``restricted`` |
| from then on, or an ``uncollectible`` invoice | ``suspended`` |

- A payment still clearing (``pending``, created in the last
  ``SKELETON_BILLING_PENDING_PAYMENT_HOLD_DAYS``) holds the level at ``past_due``: a boleto or
  Pix in flight never blocks a paying customer.
- An extension (``access_override_until``, with a reason, set by the platform) keeps the level
  ``active`` until it expires.
- After ``SKELETON_BILLING_CANCEL_AFTER_DAYS`` the subscription is canceled and the overdue
  invoices become ``uncollectible``; the organization stays suspended and no data is deleted.
  Paying an uncollectible invoice (offline, by the platform) clears it.
- Auto-charge subscriptions retry the collection ``SKELETON_BILLING_RETRY_SCHEDULE_DAYS`` days
  after the due date (``1,3,5`` by default) through the collectors registered by #400.

``run`` does all of it for the current organization; the hourly ``billing.cycle`` job calls it
after the renewals, and every settled invoice refreshes the level at once.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings

from skeleton.common.entitlements import AccessLevel

from . import engine

# Collectors try to charge an invoice again: (invoice, now) -> None. The provider module registers one.
_collectors: list[Callable[[Any, datetime], None]] = []
TRIAL_NOTICE_DAYS = 3


def register_collector(collector: Callable[[Any, datetime], None]) -> None:
    if collector not in _collectors:
        _collectors.append(collector)


def _setting(name: str, default: int) -> int:
    return int(getattr(settings, name, default))


def thresholds() -> tuple[int, int, int]:
    grace = _setting("SKELETON_BILLING_GRACE_DAYS", 7)
    suspend = max(grace, _setting("SKELETON_BILLING_SUSPEND_AFTER_DAYS", 30))
    cancel = max(suspend, _setting("SKELETON_BILLING_CANCEL_AFTER_DAYS", 60))
    return grace, suspend, cancel


def retry_schedule() -> tuple[int, ...]:
    raw = str(getattr(settings, "SKELETON_BILLING_RETRY_SCHEDULE_DAYS", "1,3,5") or "")
    return tuple(sorted({int(part) for part in raw.split(",") if part.strip()}))


def _oldest_overdue(now: datetime) -> Any:
    from .models import Invoice

    return Invoice.objects.filter(status=Invoice.Status.OPEN, due_at__lt=now).order_by("due_at", "number").first()


def _pending_payment(invoice: Any, now: datetime) -> bool:
    hold = timedelta(days=_setting("SKELETON_BILLING_PENDING_PAYMENT_HOLD_DAYS", 3))
    return invoice.payments.filter(status="pending", created_at__gte=now - hold).exists()


def derived_level(billing_account: Any, *, now: datetime) -> AccessLevel:
    from .models import Invoice

    if billing_account.access_override_until and billing_account.access_override_until > now:
        return AccessLevel.ACTIVE
    if Invoice.objects.filter(status=Invoice.Status.UNCOLLECTIBLE).exists():
        return AccessLevel.SUSPENDED
    invoice = _oldest_overdue(now)
    if invoice is None:
        return AccessLevel.ACTIVE
    grace, suspend, _ = thresholds()
    days = (now - invoice.due_at) // timedelta(days=1)
    if days < grace:
        return AccessLevel.PAST_DUE
    level = AccessLevel.RESTRICTED if days < suspend else AccessLevel.SUSPENDED
    return AccessLevel.PAST_DUE if _pending_payment(invoice, now) else level


def refresh_access(*, now: datetime | None = None) -> AccessLevel:
    """Store the derived level when it changed, with the event and the notice of the new level."""
    from . import notices

    now = engine._now(now)
    billing_account = engine.account(lock=True)
    if billing_account is None:
        return AccessLevel.ACTIVE
    level = derived_level(billing_account, now=now)
    if level.value == billing_account.access_state:
        return level
    previous = billing_account.access_state
    billing_account.access_state = level.value
    billing_account.access_state_since = now
    billing_account.save(update_fields={"access_state", "access_state_since"})
    engine._publish("billing.access_state_changed", billing_account, {"access_state": level.value})
    engine.forget()
    notices.access_changed(billing_account, previous=previous, now=now)
    return level


def _retry(now: datetime) -> None:
    from .models import Invoice, Subscription

    schedule = retry_schedule()
    due = Invoice.objects.select_for_update().filter(
        status=Invoice.Status.OPEN,
        next_attempt_at__lte=now,
        subscription__collection=Subscription.Collection.AUTO_CHARGE,
    )
    for invoice in due.order_by("due_at", "number"):
        invoice.attempt_count += 1
        later = [days for days in schedule if invoice.due_at + timedelta(days=days) > now]
        invoice.next_attempt_at = invoice.due_at + timedelta(days=later[0]) if later else None
        invoice.save(update_fields={"attempt_count", "next_attempt_at"})
        for collector in _collectors:
            collector(invoice, now)


def schedule_first_retry(invoice: Any) -> None:
    """Called when an auto-charge attempt failed: the next retry follows the schedule."""
    schedule = retry_schedule()
    if invoice.status == "open" and schedule and invoice.next_attempt_at is None:
        invoice.next_attempt_at = invoice.due_at + timedelta(days=schedule[0])
        invoice.save(update_fields={"next_attempt_at"})


def _cancel_uncollectible(now: datetime) -> None:
    from .models import Invoice

    _, _, cancel = thresholds()
    overdue = Invoice.objects.select_for_update().filter(
        status=Invoice.Status.OPEN, due_at__lte=now - timedelta(days=cancel)
    )
    if not overdue.exists():
        return
    for invoice in overdue:
        invoice.status = Invoice.Status.UNCOLLECTIBLE
        invoice.save(update_fields={"status"})
    subscription = engine.current_subscription(lock=True)
    if subscription is not None:
        engine._cancel_now(subscription, at=now)
        engine._subscription_changed(subscription)
    engine.forget()


def run(*, now: datetime | None = None) -> AccessLevel:
    from . import notices

    now = engine._now(now)
    if engine.account() is None:
        return AccessLevel.ACTIVE
    _retry(now)
    _cancel_uncollectible(now)
    notices.trial_ending(now=now, days=TRIAL_NOTICE_DAYS)
    invoice = _oldest_overdue(now)
    if invoice is not None:
        notices.invoice_overdue(invoice)
    return refresh_access(now=now)


def after_settlement(billing_account: Any, now: datetime) -> None:
    refresh_access(now=now)


def register_dunning() -> None:
    engine.register_after_settlement(after_settlement)
