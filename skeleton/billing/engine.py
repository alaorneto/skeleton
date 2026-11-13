"""Subscription engine (#396): start, change, renew and settle, in the current organization.

Every function runs in the context of one organization (a tenant context for a person, a
``system_context`` for jobs) inside the caller's transaction, and takes ``now`` so tests and
jobs decide the clock. The engine never calls a payment provider: it issues invoices, and a
paid invoice (zero-total, settled by a provider in #400 or registered offline) turns its
pending items into rights. Rights are what the entitlement policy reads (``entitlements.py``).

- **Start:** the first price starts the subscription and fixes its currency and cycle. With
  trial days (once per organization and product) it starts ``trialing``; otherwise it starts
  ``incomplete`` with an invoice of the first period.
- **Add an app or a storage pack:** at once, with a proration line for the rest of the period;
  the item stays ``pending`` until the invoice is paid. During the subscription's trial, or the
  add-on's own trial, it is active and free.
- **Upgrade:** at once, the new price prorated and the unused old one credited on the same
  invoice; the old item ends when the invoice is paid. **Downgrade:** at the end of the period.
- **Remove:** at the end of the period, without refund. A pending (unpaid) item ends at once and
  its open invoice is voided when it only charged that item.
- **Incomplete:** a subscription whose first invoice is unpaid does not renew; paying it late
  restarts the period at the payment.
- **Renew:** on the period end, items are billed for the new period (prorated when a trial ends
  inside it) together with the lines that other modules contribute (overage, discounts).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.tenancy.context import current_context, request_cache

from . import money
from .periods import period_end, require_aware, seconds_between

logger = logging.getLogger("skeleton.billing")
MEMO_KEY = "sk_billing.summary"


@dataclass
class LineDraft:
    """An invoice line before the invoice exists."""

    kind: str
    description: str
    amount: int
    unit_amount: int
    quantity: int = 1
    price: Any = None
    app_label: str = ""
    period_start: datetime | None = None
    period_end: datetime | None = None
    item: Any = None


# Contributions to a renewal invoice: (subscription, closing period start, closing period end,
# lines so far, now) -> extra lines. Registered in code by the quota (#398) and voucher (#397) modules.
LineSource = Callable[[Any, datetime, datetime, list[LineDraft], datetime], list[LineDraft]]
_line_sources: list[tuple[int, LineSource]] = []
# Called after a payment settled an invoice: (account, now). The dunning module (#399) registers one.
_after_settlement: list[Callable[[Any, datetime], None]] = []


def register_line_source(source: LineSource, *, order: int = 100) -> None:
    """Add ``source`` to every renewal invoice; lower ``order`` runs first (discounts run last)."""
    if any(existing is source for _, existing in _line_sources):
        return
    _line_sources.append((order, source))
    _line_sources.sort(key=lambda entry: entry[0])


# Called after an invoice was issued: (invoice). The notices module (#399) registers one.
_after_issue: list[Callable[[Any], None]] = []


def register_after_issue(callback: Callable[[Any], None]) -> None:
    if callback not in _after_issue:
        _after_issue.append(callback)


# Called after an invoice was paid: (invoice). The notices module (#399) registers one.
_after_payment: list[Callable[[Any], None]] = []


def register_after_payment(callback: Callable[[Any], None]) -> None:
    if callback not in _after_payment:
        _after_payment.append(callback)


def register_after_settlement(callback: Callable[[Any, datetime], None]) -> None:
    if callback not in _after_settlement:
        _after_settlement.append(callback)


def billing_enabled() -> bool:
    return bool(getattr(settings, "SKELETON_BILLING_ENABLED", False))


def default_currency() -> str:
    return money.validate_currency(getattr(settings, "SKELETON_BILLING_DEFAULT_CURRENCY", "BRL"))


def payment_terms_days() -> int:
    return int(getattr(settings, "SKELETON_BILLING_PAYMENT_TERMS_DAYS", 7))


def forget() -> None:
    """Drop the memoized rights of this request after a billing write."""
    from skeleton.system.applications import forget_application_states

    cache = request_cache()
    if cache is not None:
        for key in [key for key in cache if isinstance(key, tuple) and key[:1] == (MEMO_KEY,)]:
            del cache[key]
    forget_application_states(everywhere=True)


def clock() -> datetime:
    """The billing clock: the time rights and renewals are judged at (tests replace it)."""
    return timezone.now()


def _now(now: datetime | None) -> datetime:
    return require_aware(now or clock())


def _organization_id() -> Any:
    organization_id = current_context().organization_id
    if organization_id is None:
        from skeleton.common.exceptions import MissingTenantContext

        raise MissingTenantContext("Billing works on one organization.")
    return organization_id


def _invalid(field: str, message: str, code: str) -> ValidationError:
    return ValidationError({field: ValidationError(message, code=code)})


def _publish(code: str, record: Any, data: dict[str, Any]) -> None:
    from skeleton.events.services import publish_event

    publish_event(code, record=record, data=data)


# Accounts and lookups ----------------------------------------------------------------------


def account(*, lock: bool = False) -> Any:
    from .models import BillingAccount

    queryset = BillingAccount.objects.all()
    if lock:
        queryset = queryset.select_for_update()
    return queryset.first()


def ensure_account(*, currency: str | None = None) -> Any:
    """The organization's billing account, created on first use (serialized on the organization)."""
    from skeleton.access.services import organization_lock

    from .models import BillingAccount

    existing = account(lock=True)
    if existing is not None:
        return existing
    with organization_lock() as organization:
        existing = account(lock=True)
        if existing is None:
            existing = BillingAccount(organization=organization, currency=currency or default_currency())
            existing.save()
    return existing


def current_subscription(*, lock: bool = False) -> Any:
    from .models import Subscription

    queryset = Subscription.objects.exclude(status=Subscription.Status.CANCELED)
    if lock:
        queryset = queryset.select_for_update()
    return queryset.first()


def default_price(currency: str) -> Any:
    from .models import Price, Product

    return (
        Price.objects.filter(is_default=True, is_active=True, currency=currency, product__kind=Product.Kind.BASE_PLAN)
        .select_related("product")
        .first()
    )


def trial_used(product_id: Any) -> bool:
    from .models import SubscriptionItem

    return SubscriptionItem.objects.filter(price__product_id=product_id, trial_end__isnull=False).exists()


def has_payment_method(billing_account: Any) -> bool:
    """Whether a reusable payment method is on file; providers fill it in #400."""
    return bool(getattr(billing_account, "payment_method_ref", ""))


def live_items(subscription: Any, *, now: datetime, statuses: tuple[str, ...] = ("active",)) -> list[Any]:
    """Items in ``statuses`` that started and did not end at ``now``."""
    from django.db.models import Q

    return list(
        subscription.items.filter(status__in=statuses, starts_at__lte=now)
        .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        .select_related("price", "price__product")
        .order_by("created_at", "sysid")
    )


# Invoices -----------------------------------------------------------------------------------


def issue_invoice(
    billing_account: Any,
    subscription: Any,
    lines: list[LineDraft],
    *,
    now: datetime,
    period: tuple[datetime, datetime] | None = None,
    due_at: datetime | None = None,
) -> Any:
    """Number and write an invoice with its lines; a zero total is paid at once."""
    from .models import Invoice, InvoiceLine
    from .numbering import next_invoice_number
    from .tax import tax_calculator

    currency = subscription.currency if subscription is not None else billing_account.currency
    charges = sum(line.amount for line in lines if line.kind != InvoiceLine.Kind.DISCOUNT)
    discounts = -sum(line.amount for line in lines if line.kind == InvoiceLine.Kind.DISCOUNT)
    if charges < 0 or discounts < 0:
        raise ValueError("An invoice cannot owe the organization money.")
    discount_total = min(discounts, charges)
    tax_total = int(tax_calculator().tax(account=billing_account, currency=currency, lines=lines))
    if tax_total < 0:
        raise ValueError("Tax cannot be negative.")
    invoice = Invoice(
        organization_id=billing_account.organization_id,
        account=billing_account,
        subscription=subscription,
        number=next_invoice_number(at=now),
        currency=currency,
        subtotal=charges,
        discount_total=discount_total,
        tax_total=tax_total,
        total=charges - discount_total + tax_total,
        period_start=period[0] if period else None,
        period_end=period[1] if period else None,
        issued_at=now,
        due_at=due_at or now + timedelta(days=payment_terms_days()),
    )
    invoice.save()
    for line in lines:
        InvoiceLine(
            organization_id=billing_account.organization_id,
            invoice=invoice,
            kind=line.kind,
            description=line.description[:300],
            price=line.price,
            subscription_item=line.item,
            app_label=line.app_label,
            quantity=line.quantity,
            unit_amount=line.unit_amount,
            amount=line.amount,
            period_start=line.period_start,
            period_end=line.period_end,
        ).save()
    _publish(
        "billing.invoice_finalized",
        invoice,
        {"number": invoice.number, "status": invoice.status, "currency": currency, "total": invoice.total},
    )
    for callback in _after_issue:
        callback(invoice)
    if invoice.total == 0:
        settle_invoice(invoice, now=now)
    return invoice


def settle_invoice(invoice: Any, *, now: datetime | None = None) -> Any:
    """Mark ``invoice`` paid and turn the items it charged into rights; idempotent."""
    from .models import Invoice, Subscription, SubscriptionItem

    now = _now(now)
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == Invoice.Status.PAID:
        return invoice
    # An uncollectible invoice can still be paid (offline, by the platform): it clears the debt.
    if invoice.status not in (Invoice.Status.OPEN, Invoice.Status.UNCOLLECTIBLE):
        raise _invalid("invoice", gettext("Só uma fatura em aberto pode ser paga."), "invoice_not_open")
    invoice.status, invoice.amount_paid, invoice.paid_at = Invoice.Status.PAID, invoice.total, now
    invoice.next_attempt_at = None
    invoice.save(update_fields={"status", "amount_paid", "paid_at", "next_attempt_at"})
    activated = []
    for line in invoice.lines.filter(subscription_item__isnull=False).select_related("subscription_item"):
        item = SubscriptionItem.objects.select_for_update().get(pk=line.subscription_item_id)
        if item.status != SubscriptionItem.Status.PENDING:
            continue
        item.status = SubscriptionItem.Status.ACTIVE
        item.save(update_fields={"status"})
        activated.append(item)
        if item.replaces_id:
            _end_item(SubscriptionItem.objects.select_for_update().get(pk=item.replaces_id), at=now)
    subscription = invoice.subscription
    if subscription is not None:
        subscription = Subscription.objects.select_for_update().get(pk=subscription.pk)
        if subscription.status == Subscription.Status.INCOMPLETE:
            subscription.status = Subscription.Status.ACTIVE
            fields = {"status"}
            if now > subscription.current_period_start:
                # A late first payment starts the paid period now: no charge for months without access.
                subscription.billing_anchor_day = now.day
                subscription.current_period_start = now
                subscription.current_period_end = period_end(
                    now, interval=subscription.interval, interval_count=subscription.interval_count, anchor_day=now.day
                )
                fields |= {"billing_anchor_day", "current_period_start", "current_period_end"}
            subscription.save(update_fields=fields)
            _subscription_changed(subscription)
    _publish(
        "billing.invoice_paid",
        invoice,
        {"number": invoice.number, "status": invoice.status, "currency": invoice.currency, "total": invoice.total},
    )
    forget()
    for callback in _after_settlement:
        callback(invoice.account, now)
    for paid in _after_payment:
        paid(invoice)
    activate_apps([item.app_label for item in activated if item.app_label])
    return invoice


def void_invoice(invoice: Any, *, now: datetime | None = None) -> Any:
    """Cancel an open invoice; the pending items it charged end without rights."""
    from .models import Invoice, SubscriptionItem

    now = _now(now)
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == Invoice.Status.VOID:
        return invoice
    if invoice.status != Invoice.Status.OPEN:
        raise _invalid("invoice", gettext("Só uma fatura em aberto pode ser anulada."), "invoice_not_open")
    invoice.status, invoice.voided_at = Invoice.Status.VOID, now
    invoice.save(update_fields={"status", "voided_at"})
    for line in invoice.lines.filter(subscription_item__isnull=False):
        item = SubscriptionItem.objects.select_for_update().get(pk=line.subscription_item_id)
        if item.status == SubscriptionItem.Status.PENDING:
            _end_item(item, at=now)
    forget()
    return invoice


# Subscriptions and items ----------------------------------------------------------------------


def _subscription_changed(subscription: Any) -> None:
    _publish("billing.subscription_changed", subscription, {"status": subscription.status})


def _end_item(item: Any, *, at: datetime) -> None:
    from .models import SubscriptionItem

    item.status = SubscriptionItem.Status.ENDED
    item.ends_at = max(at, item.starts_at)
    item.save(update_fields={"status", "ends_at"})


def _require_price(price: Any) -> None:
    if not price.is_active or price.deleted_at is not None or price.product.deleted_at is not None:
        raise _invalid("price", gettext("Este preço não está mais à venda."), "price_inactive")


def _line(kind: str, item: Any, amount: int, *, start: datetime, end: datetime) -> LineDraft:
    price = item.price
    return LineDraft(
        kind=kind,
        description=price.product.name,
        amount=amount,
        unit_amount=price.unit_amount,
        quantity=item.quantity,
        price=price,
        app_label=item.app_label,
        period_start=start,
        period_end=end,
        item=item,
    )


def _prorated(full: int, subscription: Any, *, start: datetime, end: datetime | None = None) -> int:
    end = end or subscription.current_period_end
    total = seconds_between(subscription.current_period_start, subscription.current_period_end)
    return money.scale(full, seconds_between(start, end), total) if total else 0


def start_subscription(price: Any, *, now: datetime | None = None, quantity: int = 1) -> Any:
    """Start the organization's subscription with ``price``; fixes its currency and cycle."""
    from .models import Product, Subscription, SubscriptionItem

    now = _now(now)
    _require_price(price)
    billing_account = ensure_account(currency=price.currency)
    if price.currency != billing_account.currency:
        raise _invalid("price", gettext("O preço está em outra moeda que a da conta."), "currency_mismatch")
    if current_subscription(lock=True) is not None:
        raise _invalid("price", gettext("A organização já tem uma assinatura."), "subscription_exists")
    requires_method = bool(getattr(settings, "SKELETON_BILLING_TRIAL_REQUIRES_PAYMENT_METHOD", False))
    trial = (
        price.trial_days > 0
        and not trial_used(price.product_id)
        and (not requires_method or has_payment_method(billing_account))
    )
    start = now
    end = now + timedelta(days=price.trial_days) if trial else None
    first_paid = end or start
    subscription = Subscription(
        organization_id=billing_account.organization_id,
        account=billing_account,
        status=Subscription.Status.TRIALING if trial else Subscription.Status.INCOMPLETE,
        currency=price.currency,
        interval=price.interval,
        interval_count=price.interval_count,
        billing_anchor_day=first_paid.day,
        current_period_start=start,
        current_period_end=end
        or period_end(start, interval=price.interval, interval_count=price.interval_count, anchor_day=start.day),
        trial_end=end,
    )
    subscription.save()
    product = price.product
    item = SubscriptionItem(
        organization_id=billing_account.organization_id,
        subscription=subscription,
        price=price,
        app_label=product.app_label if product.kind == Product.Kind.APP else "",
        quantity=quantity,
        status=SubscriptionItem.Status.ACTIVE if trial else SubscriptionItem.Status.PENDING,
        starts_at=start,
        trial_end=end,
    )
    item.save()
    _subscription_changed(subscription)
    forget()
    if trial:
        activate_apps([item.app_label] if item.app_label else [])
    else:
        full = price.unit_amount * quantity
        issue_invoice(
            billing_account,
            subscription,
            [_line("subscription", item, full, start=start, end=subscription.current_period_end)],
            now=now,
            period=(start, subscription.current_period_end),
            due_at=now,
        )
    return subscription


def start_default_subscription(*, now: datetime | None = None) -> Any:
    """The default plan for an organization without a subscription; nothing without a default price."""
    if current_subscription() is not None:
        return None
    billing_account = ensure_account()
    price = default_price(billing_account.currency)
    if price is None:
        return None
    return start_subscription(price, now=now)


def _require_cycle(subscription: Any, price: Any) -> None:
    if (price.currency, price.interval, price.interval_count) != (
        subscription.currency,
        subscription.interval,
        subscription.interval_count,
    ):
        raise _invalid("price", gettext("O preço precisa ter a moeda e o ciclo da assinatura."), "price_cycle_mismatch")


def add_item(price: Any, *, now: datetime | None = None, quantity: int = 1) -> Any:
    """Contract an app or a storage pack; starts the subscription when there is none."""
    from .models import Product, SubscriptionItem

    now = _now(now)
    _require_price(price)
    product = price.product
    if product.kind == Product.Kind.BASE_PLAN:
        raise _invalid("price", gettext("Use a troca de plano para o plano base."), "use_change_plan")
    if type(quantity) is not int or quantity < 1:
        raise _invalid("quantity", gettext("Use uma quantidade inteira positiva."), "invalid_quantity")
    subscription = current_subscription(lock=True)
    if subscription is None:
        return start_subscription(price, now=now, quantity=quantity).items.get()
    _require_cycle(subscription, price)
    if product.kind == Product.Kind.APP:
        if product.app_label in included_apps(subscription, now=now):
            raise _invalid("price", gettext("O aplicativo já está incluído no plano."), "included_in_plan")
        if any(
            item.app_label == product.app_label
            for item in live_items(subscription, now=now, statuses=("active", "pending"))
        ):
            raise _invalid("price", gettext("O aplicativo já foi contratado."), "already_subscribed")
    trialing = subscription.status == subscription.Status.TRIALING
    own_trial = not trialing and price.trial_days > 0 and not trial_used(product.pk)
    trial_end = subscription.trial_end if trialing else (now + timedelta(days=price.trial_days) if own_trial else None)
    item = SubscriptionItem(
        organization_id=subscription.organization_id,
        subscription=subscription,
        price=price,
        app_label=product.app_label if product.kind == Product.Kind.APP else "",
        quantity=quantity,
        status=SubscriptionItem.Status.ACTIVE if trial_end else SubscriptionItem.Status.PENDING,
        starts_at=now,
        trial_end=trial_end,
    )
    item.save()
    forget()
    if trial_end:
        activate_apps([item.app_label] if item.app_label else [])
        return item
    amount = _prorated(price.unit_amount * quantity, subscription, start=now)
    issue_invoice(
        subscription.account,
        subscription,
        [_line("proration", item, amount, start=now, end=subscription.current_period_end)],
        now=now,
        due_at=now,
    )
    item.refresh_from_db()
    return item


def remove_item(item: Any, *, now: datetime | None = None) -> Any:
    """End an app or storage pack: at the period end, or at once while unpaid or free in a trial."""
    from .models import Invoice, Product, SubscriptionItem

    now = _now(now)
    item = SubscriptionItem.objects.select_for_update().select_related("price__product", "subscription").get(pk=item.pk)
    if item.price.product.kind == Product.Kind.BASE_PLAN:
        raise _invalid("item", gettext("Cancele a assinatura para encerrar o plano base."), "use_cancel")
    if item.status == SubscriptionItem.Status.ENDED:
        return item
    if item.status == SubscriptionItem.Status.PENDING:
        _end_item(item, at=now)
        for invoice in Invoice.objects.filter(status=Invoice.Status.OPEN, lines__subscription_item=item).distinct():
            if not invoice.lines.exclude(subscription_item=item).exists():
                void_invoice(invoice, now=now)
    elif item.trial_end and item.trial_end > now:
        _end_item(item, at=now)
    else:
        item.ends_at = item.subscription.current_period_end
        item.save(update_fields={"ends_at"})
    forget()
    return item


def change_plan(price: Any, *, now: datetime | None = None) -> Any:
    """Switch the base plan: an upgrade at once (prorated), a downgrade at the period end."""
    from .models import Product, Subscription, SubscriptionItem

    now = _now(now)
    _require_price(price)
    if price.product.kind != Product.Kind.BASE_PLAN:
        raise _invalid("price", gettext("Escolha um preço de plano base."), "not_a_plan")
    subscription = current_subscription(lock=True)
    if subscription is None:
        return start_subscription(price, now=now)
    _require_cycle(subscription, price)
    current = [item for item in live_items(subscription, now=now) if item.price.product.kind == Product.Kind.BASE_PLAN]
    old = current[0] if current else None
    if old is not None and old.price_id == price.pk:
        if subscription.scheduled_price_id:
            subscription.scheduled_price = None
            subscription.save(update_fields={"scheduled_price"})
        return subscription
    trialing = subscription.status == Subscription.Status.TRIALING
    if old is not None and price.unit_amount <= old.price.unit_amount and not trialing:
        subscription.scheduled_price = price
        subscription.save(update_fields={"scheduled_price"})
        _subscription_changed(subscription)
        return subscription
    item = SubscriptionItem(
        organization_id=subscription.organization_id,
        subscription=subscription,
        price=price,
        quantity=1,
        status=SubscriptionItem.Status.ACTIVE if trialing else SubscriptionItem.Status.PENDING,
        starts_at=now,
        trial_end=subscription.trial_end if trialing else None,
        replaces=old,
    )
    item.save()
    if subscription.scheduled_price_id:
        subscription.scheduled_price = None
        subscription.save(update_fields={"scheduled_price"})
    if trialing:
        if old is not None:
            _end_item(old, at=now)
        forget()
        _subscription_changed(subscription)
        return subscription
    lines = [
        _line(
            "proration",
            item,
            _prorated(price.unit_amount, subscription, start=now),
            start=now,
            end=subscription.current_period_end,
        )
    ]
    if old is not None:
        unused = _prorated(old.price.unit_amount * old.quantity, subscription, start=now)
        lines.append(_line("adjustment", old, -unused, start=now, end=subscription.current_period_end))
    issue_invoice(subscription.account, subscription, lines, now=now, due_at=now)
    forget()
    _subscription_changed(subscription)
    return subscription


def cancel(*, now: datetime | None = None, at_period_end: bool = True) -> Any:
    """Cancel the subscription: at the period end by default, at once during a trial or on request."""
    from .models import Subscription

    now = _now(now)
    subscription = current_subscription(lock=True)
    if subscription is None:
        raise _invalid("subscription", gettext("Não há assinatura ativa."), "no_subscription")
    if at_period_end and subscription.status != Subscription.Status.TRIALING:
        subscription.cancel_at_period_end = True
        subscription.save(update_fields={"cancel_at_period_end"})
    else:
        _cancel_now(subscription, at=now)
    _subscription_changed(subscription)
    forget()
    return subscription


def resume() -> Any:
    subscription = current_subscription(lock=True)
    if subscription is None or not subscription.cancel_at_period_end:
        return subscription
    subscription.cancel_at_period_end = False
    subscription.save(update_fields={"cancel_at_period_end"})
    _subscription_changed(subscription)
    return subscription


def _cancel_now(subscription: Any, *, at: datetime) -> None:
    from .models import Subscription

    for item in live_items(subscription, now=at, statuses=("active", "pending")):
        _end_item(item, at=at)
    subscription.status = Subscription.Status.CANCELED
    subscription.canceled_at = at
    subscription.save(update_fields={"status", "canceled_at"})


def included_apps(subscription: Any, *, now: datetime) -> set[str]:
    from .models import Product

    labels: set[str] = set()
    for item in live_items(subscription, now=now):
        if item.price.product.kind == Product.Kind.BASE_PLAN:
            labels.update(item.price.included_apps)
    return labels


def renew_due(*, now: datetime | None = None) -> Any:
    """Close the current period if it ended: renew and invoice it, or cancel; ``None`` if not due."""
    from .models import Product, Subscription, SubscriptionItem

    now = _now(now)
    subscription = current_subscription(lock=True)
    if subscription is None or now < subscription.current_period_end:
        return None
    if subscription.status == Subscription.Status.INCOMPLETE:
        # Never paid: it does not renew; the first payment restarts it, dunning cancels it (#399).
        return None
    start = subscription.current_period_end
    closing = (subscription.current_period_start, start)
    if subscription.cancel_at_period_end:
        _cancel_now(subscription, at=start)
        _subscription_changed(subscription)
        forget()
        return None
    end = period_end(
        start,
        interval=subscription.interval,
        interval_count=subscription.interval_count,
        anchor_day=subscription.billing_anchor_day,
    )
    if subscription.scheduled_price_id:
        scheduled = subscription.scheduled_price
        for item in live_items(subscription, now=start):
            if item.price.product.kind == Product.Kind.BASE_PLAN:
                _end_item(item, at=start)
        SubscriptionItem(
            organization_id=subscription.organization_id,
            subscription=subscription,
            price=scheduled,
            status=SubscriptionItem.Status.ACTIVE,
            starts_at=start,
        ).save()
        subscription.scheduled_price = None
    for item in subscription.items.filter(status=SubscriptionItem.Status.ACTIVE, ends_at__lte=start):
        item.status = SubscriptionItem.Status.ENDED
        item.save(update_fields={"status"})
    was_trial = subscription.status == Subscription.Status.TRIALING
    subscription.current_period_start, subscription.current_period_end = start, end
    if was_trial:
        subscription.status = Subscription.Status.ACTIVE
    subscription.save(
        update_fields={"current_period_start", "current_period_end", "status", "scheduled_price", "updated_at"}
    )
    lines = []
    for item in live_items(subscription, now=start):
        billable = max(start, item.trial_end or start, item.starts_at)
        if billable >= end:
            continue
        full = item.price.unit_amount * item.quantity
        if billable == start:
            lines.append(_line("subscription", item, full, start=start, end=end))
        else:
            lines.append(
                _line("proration", item, _prorated(full, subscription, start=billable), start=billable, end=end)
            )
    for _, source in _line_sources:
        lines.extend(source(subscription, closing[0], closing[1], lines, now))
    if was_trial:
        _subscription_changed(subscription)
    forget()
    if not lines:
        return None
    return issue_invoice(subscription.account, subscription, lines, now=now, period=(start, end), due_at=start)


# Apps -------------------------------------------------------------------------------------------


def activate_apps(labels: list[str]) -> None:
    """Turn on the apps that a new right covers, dependencies first; a refusal is logged, never raised."""
    from skeleton.system.app_registry import get_app, required_labels
    from skeleton.system.applications import activate_for_entitlement

    for label in dict.fromkeys(labels):
        try:
            get_app(label)
        except KeyError:
            continue
        for target in (*required_labels(label), label):
            try:
                with transaction.atomic():
                    activate_for_entitlement(target)
            except ValidationError as error:
                logger.info(
                    "billing.app_not_activated", extra={"app_label": target, "codes": sorted(error.message_dict)}
                )
                break
