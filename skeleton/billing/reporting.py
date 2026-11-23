"""Read-only billing figures of the organization of the context (#710, #711).

For the insights snapshot and the platform API: plain dictionaries of numbers and states, in
the smallest unit of the currency, never a provider reference, a payment method or a secret.
Every function reads the organization of the current context (a read-only system context
opened by the caller); with billing off, they return nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.conf import settings
from django.utils import timezone


def enabled() -> bool:
    return bool(getattr(settings, "SKELETON_BILLING_ENABLED", False))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def monthly_amount(price: Any, quantity: int) -> int:
    """The recurring amount of an item normalized to one month (MRR), rounded half up."""
    months = price.interval_count * (12 if price.interval == "year" else 1)
    return (price.unit_amount * quantity * 2 + months) // (2 * months)


def organization_billing_summary(*, now: datetime | None = None) -> dict[str, Any]:
    """Access state, subscription, MRR, open and overdue amounts of the context organization; {} without billing."""
    if not enabled():
        return {}
    from .engine import current_subscription, live_items
    from .models import BillingAccount, Invoice, Payment

    now = now or timezone.now()
    account = BillingAccount.objects.first()
    if account is None:
        return {}
    subscription = current_subscription()
    items = live_items(subscription, now=now) if subscription is not None else []
    plan = next((item.price.product.code for item in items if item.price.product.kind == "base_plan"), "")
    mrr = sum(monthly_amount(item.price, item.quantity) for item in items)
    open_invoices = list(Invoice.objects.filter(status=Invoice.Status.OPEN).values("total", "amount_paid", "due_at"))
    open_amount = sum(max(row["total"] - row["amount_paid"], 0) for row in open_invoices)
    overdue = [row for row in open_invoices if row["due_at"] < now]
    last_payment = (
        Payment.objects.filter(status=Payment.Status.SUCCEEDED)
        .order_by("-succeeded_at")
        .values_list("succeeded_at", flat=True)
        .first()
    )
    return {
        "access_state": account.access_state,
        "subscription_status": subscription.status if subscription is not None else "",
        "currency": subscription.currency if subscription is not None else account.currency,
        "plan_code": plan,
        "mrr": mrr,
        "open_amount": open_amount,
        "overdue_amount": sum(max(row["total"] - row["amount_paid"], 0) for row in overdue),
        "oldest_due_at": _iso(min((row["due_at"] for row in overdue), default=None)),
        "last_payment_at": _iso(last_payment),
        "trial_ends_at": _iso(subscription.trial_end if subscription is not None else None),
        "current_period_end": _iso(subscription.current_period_end if subscription is not None else None),
    }


def day_totals(*, start: datetime, end: datetime) -> dict[str, dict[str, int]]:
    """Invoiced, paid and refunded in ``[start, end)`` by currency, for the daily metrics."""
    if not enabled():
        return {}
    from django.db.models import Sum

    from .models import Invoice, Payment

    result: dict[str, dict[str, int]] = {}

    def add(code: str, rows: Any) -> None:
        for row in rows:
            result.setdefault(code, {})[row["currency"]] = int(row["value"] or 0)

    add(
        "billing.invoiced",
        Invoice.objects.filter(issued_at__gte=start, issued_at__lt=end)
        .exclude(status=Invoice.Status.VOID)
        .values("currency")
        .annotate(value=Sum("total")),
    )
    add(
        "billing.paid",
        Payment.objects.filter(status=Payment.Status.SUCCEEDED, succeeded_at__gte=start, succeeded_at__lt=end)
        .values("currency")
        .annotate(value=Sum("amount")),
    )
    add(
        "billing.refunded",
        Payment.objects.filter(status=Payment.Status.REFUNDED, updated_at__gte=start, updated_at__lt=end)
        .values("currency")
        .annotate(value=Sum("amount")),
    )
    return result


def organization_invoices(*, status: str | None = None, issued_since: datetime | None = None) -> list[dict[str, Any]]:
    """The invoices of the context organization, newest first; never lines with provider data."""
    if not enabled():
        return []
    from .models import Invoice

    rows = Invoice.objects.all()
    if status:
        rows = rows.filter(status=status)
    if issued_since is not None:
        rows = rows.filter(issued_at__gte=issued_since)
    return [
        {
            "id": str(row.pk),
            "number": row.number,
            "status": row.status,
            "currency": row.currency,
            "subtotal": row.subtotal,
            "discount": row.discount_total,
            "tax": row.tax_total,
            "total": row.total,
            "paid": row.amount_paid,
            "period_start": _iso(row.period_start),
            "period_end": _iso(row.period_end),
            "issued_at": _iso(row.issued_at),
            "due_at": _iso(row.due_at),
            "paid_at": _iso(row.paid_at),
        }
        for row in rows.order_by("-issued_at", "sysid")
    ]


def organization_payments() -> list[dict[str, Any]]:
    """The payments of the context organization; never the provider reference or the payment method data."""
    if not enabled():
        return []
    from .models import Payment

    return [
        {
            "id": str(row.pk),
            "invoice_id": str(row.invoice_id),
            "method": row.method,
            "status": row.status,
            "amount": row.amount,
            "currency": row.currency,
            "provider": row.provider,
            "created_at": row.created_at.isoformat(),
            "confirmed_at": _iso(row.succeeded_at),
        }
        for row in Payment.objects.order_by("-created_at", "sysid")
    ]


def organization_billing_account() -> dict[str, Any] | None:
    """The billing contact of the context organization (personal data), or ``None``."""
    if not enabled():
        return None
    from .models import BillingAccount

    account = BillingAccount.objects.first()
    if account is None:
        return None
    return {
        "legal_name": account.legal_name,
        "billing_email": account.billing_email,
        "tax_id": account.tax_id,
        "country": account.country,
        "currency": account.currency,
        "access_state": account.access_state,
    }
