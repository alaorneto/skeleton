"""Collecting invoices through the payment providers (#400).

- **Checkout:** ``create_checkout`` writes a ``CheckoutAttempt``, asks the provider for a hosted
  page with the attempt's sysid as idempotency key and stores its URL. No row is locked while
  the provider is called.
- **Auto charge:** ``billing.collect`` charges the saved method of an ``auto_charge`` account,
  with an idempotency key fixed by the invoice and its attempt number, so a worker that dies
  after the charge never charges twice. It runs right after a renewal invoice is issued and on
  the dunning retries.
- **Webhooks** (``webhooks.py``) only record a ``ProviderEvent`` and enqueue
  ``billing.process_event``, which **fetches the payment from the provider** and applies it.
- **Reconciliation:** ``billing.reconcile`` fetches the pending payments older than
  ``RECONCILE_AFTER`` that no webhook resolved.

``apply_status`` is the only place a provider's answer changes the ledger: the invoice is marked
paid only when the provider's payment names this invoice, in its currency, for the amount still
owed. Anything else leaves the invoice as it was and is logged.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from django.db import transaction

from . import engine
from .providers.base import (
    Capability,
    CustomerData,
    InvoiceData,
    PaymentState,
    PaymentStatus,
    ProviderError,
    get_provider,
    parse_invoice_reference,
)

logger = logging.getLogger("skeleton.billing")
COLLECT_JOB = "billing.collect"
PROCESS_JOB = "billing.process_event"
RECONCILE_JOB = "billing.reconcile"
RECONCILE_AFTER = timedelta(minutes=15)


def provider_account_for(billing_account: Any) -> Any:
    """The account's provider account, or the default enabled one of its currency."""
    from .models import ProviderAccount

    if billing_account.provider_account_id:
        chosen = ProviderAccount.objects.filter(pk=billing_account.provider_account_id, enabled=True).first()
        if chosen is not None:
            return chosen
    candidates = ProviderAccount.objects.filter(enabled=True).order_by("-is_default", "created_at")
    for candidate in candidates:
        if not candidate.currencies or billing_account.currency in candidate.currencies:
            return candidate
    return None


def _invoice_data(invoice: Any) -> InvoiceData:
    return InvoiceData(
        organization_id=invoice.organization_id,
        invoice_id=invoice.pk,
        number=invoice.number,
        currency=invoice.currency,
        amount=invoice.total - invoice.amount_paid,
        description=f"Fatura {invoice.number}",
        due_at=invoice.due_at,
    )


def _customer(billing_account: Any, provider_account: Any) -> str:
    provider = get_provider(provider_account.provider)
    customer = CustomerData(
        organization_id=billing_account.organization_id,
        name=billing_account.legal_name or str(billing_account.organization_id),
        email=billing_account.billing_email,
        tax_id=billing_account.tax_id,
        country=billing_account.country,
    )
    existing = (
        billing_account.provider_customer_id if billing_account.provider_account_id == provider_account.pk else ""
    )
    customer_id = provider.ensure_customer(provider_account, customer, existing_id=existing)
    if (
        customer_id != billing_account.provider_customer_id
        or billing_account.provider_account_id != provider_account.pk
    ):
        billing_account.provider_customer_id = customer_id
        billing_account.provider_account = provider_account
        billing_account.save(update_fields={"provider_customer_id", "provider_account"})
    return customer_id


def create_checkout(invoice: Any, *, return_url: str) -> Any:
    """A hosted payment page for ``invoice``; returns the ``CheckoutAttempt`` with its URL."""
    from django.core.exceptions import ValidationError
    from django.utils.translation import gettext

    from .models import CheckoutAttempt, Invoice

    if invoice.status not in (Invoice.Status.OPEN, Invoice.Status.UNCOLLECTIBLE):
        raise ValidationError({"invoice": gettext("Esta fatura não está em aberto.")})
    billing_account = invoice.account
    provider_account = provider_account_for(billing_account)
    if provider_account is None:
        raise ValidationError({"invoice": ValidationError(gettext("Pagamento indisponível."), code="no_provider")})
    provider = get_provider(provider_account.provider)
    if Capability.HOSTED_CHECKOUT not in provider.capabilities(provider_account):
        raise ValidationError({"invoice": ValidationError(gettext("Pagamento indisponível."), code="no_checkout")})
    attempt = CheckoutAttempt(
        organization_id=invoice.organization_id, invoice=invoice, provider_account=provider_account
    )
    attempt.save()
    try:
        customer_id = _customer(billing_account, provider_account)
        session = provider.create_checkout(
            provider_account,
            _invoice_data(invoice),
            customer_id=customer_id,
            return_url=return_url,
            idempotency_key=f"checkout:{attempt.pk}",
        )
    except ProviderError as exc:
        attempt.status, attempt.failure_code = CheckoutAttempt.Status.FAILED, exc.code[:64]
        attempt.save(update_fields={"status", "failure_code"})
        raise ValidationError({"invoice": ValidationError(gettext("Pagamento indisponível."), code=exc.code)}) from exc
    attempt.status, attempt.reference, attempt.url, attempt.expires_at = (
        CheckoutAttempt.Status.OPEN,
        session.reference[:200],
        session.url,
        session.expires_at,
    )
    attempt.save(update_fields={"status", "reference", "url", "expires_at"})
    return attempt


def _payment_for(invoice: Any, provider_account: Any, status: PaymentStatus) -> Any:
    from .models import Payment

    payment = (
        Payment.objects.select_for_update()
        .filter(provider=provider_account.provider, provider_reference=status.reference)
        .first()
    )
    if payment is None:
        payment = Payment(
            organization_id=invoice.organization_id,
            invoice=invoice,
            method=status.method if status.method in ("card", "pix", "boleto") else "card",
            currency=invoice.currency,
            amount=max(1, status.amount),
            provider=provider_account.provider,
            provider_reference=status.reference[:200],
        )
        payment.save()
    return payment


def apply_status(provider_account: Any, status: PaymentStatus, *, now: datetime | None = None) -> str:
    """Apply the provider's answer for one payment of the current organization; returns an outcome code."""
    from . import dunning, notices
    from .models import Invoice

    now = engine._now(now)
    parsed = parse_invoice_reference(status.invoice_reference)
    if parsed is None or parsed[0] != engine._organization_id():
        return "foreign_reference"
    invoice = Invoice.objects.select_for_update().filter(pk=parsed[1]).first()
    if invoice is None:
        return "unknown_invoice"
    owed = invoice.total - invoice.amount_paid
    if status.currency != invoice.currency or (status.status == PaymentState.SUCCEEDED and status.amount != owed):
        if invoice.status != Invoice.Status.PAID:
            logger.warning("billing.payment_mismatch", extra={"invoice": str(invoice.pk), "code": "amount_mismatch"})
            return "amount_mismatch"
    payment = _payment_for(invoice, provider_account, status)
    if payment.status == status.status.value:
        return "unchanged"
    payment.status = status.status.value
    payment.failure_code = status.failure_code[:64]
    fields = {"status", "failure_code"}
    if status.status == PaymentState.SUCCEEDED:
        payment.succeeded_at = now
        fields.add("succeeded_at")
    payment.save(update_fields=fields)
    if status.status == PaymentState.SUCCEEDED and invoice.status in (
        Invoice.Status.OPEN,
        Invoice.Status.UNCOLLECTIBLE,
    ):
        _remember_method(invoice.account, provider_account, status)
        engine.settle_invoice(invoice, now=now)
        return "paid"
    if status.status == PaymentState.FAILED:
        notices.payment_failed(invoice, attempt=invoice.attempt_count)
        dunning.schedule_first_retry(invoice)
        dunning.refresh_access(now=now)
        return "failed"
    return status.status.value


def _remember_method(billing_account: Any, provider_account: Any, status: PaymentStatus) -> None:
    if not status.method_ref:
        return
    billing_account.payment_method_ref = status.method_ref[:500]
    billing_account.payment_method_label = status.method_label[:100]
    billing_account.provider_account = provider_account
    if status.customer_id:
        billing_account.provider_customer_id = status.customer_id[:200]
    billing_account.save(
        update_fields={"payment_method_ref", "payment_method_label", "provider_account", "provider_customer_id"}
    )


def collect(invoice: Any, *, now: datetime | None = None) -> str:
    """Charge the saved method of an auto-charge account for ``invoice``."""
    from .models import Invoice, Subscription

    now = engine._now(now)
    invoice = Invoice.objects.filter(pk=invoice.pk).select_related("account", "subscription").first()
    if invoice is None or invoice.status not in (Invoice.Status.OPEN, Invoice.Status.UNCOLLECTIBLE):
        return "not_open"
    if invoice.subscription is None or invoice.subscription.collection != Subscription.Collection.AUTO_CHARGE:
        return "not_auto_charge"
    billing_account = invoice.account
    provider_account = provider_account_for(billing_account)
    if provider_account is None or not billing_account.payment_method_ref:
        return "no_method"
    provider = get_provider(provider_account.provider)
    if Capability.OFF_SESSION_CARD not in provider.capabilities(provider_account):
        return "unsupported"
    try:
        result = provider.charge_saved_method(
            provider_account,
            _invoice_data(invoice),
            customer_id=billing_account.provider_customer_id,
            method_ref=billing_account.payment_method_ref,
            idempotency_key=f"collect:{invoice.pk}:{invoice.attempt_count}",
        )
    except ProviderError as exc:
        if exc.retryable:
            raise
        result = None
        code = exc.code
    if result is None:
        status = PaymentStatus(
            reference=f"failed:{invoice.pk}:{invoice.attempt_count}",
            status=PaymentState.FAILED,
            amount=invoice.total - invoice.amount_paid,
            currency=invoice.currency,
            invoice_reference=_invoice_data(invoice).reference,
            failure_code=code,
        )
        return apply_status(provider_account, status, now=now)
    fetched = provider.fetch_payment(provider_account, result.reference)
    return apply_status(provider_account, fetched, now=now)


def collect_job(*, invoice: UUID, **payload: object) -> None:
    from .models import Invoice

    found = Invoice.objects.filter(pk=invoice).first()
    if found is not None and engine.billing_enabled():
        collect(found)


def enqueue_collect(invoice: Any) -> None:
    """After a renewal invoice is issued (after commit of its transaction, by the job queue)."""
    from skeleton.jobs.services import enqueue

    from .models import Subscription

    subscription = invoice.subscription
    if invoice.total <= 0 or subscription is None or subscription.collection != Subscription.Collection.AUTO_CHARGE:
        return
    enqueue(
        COLLECT_JOB,
        organization_id=invoice.organization_id,
        payload={"invoice": str(invoice.pk)},
        idempotency_key=f"collect:{invoice.pk}:{invoice.attempt_count}",
    )


def retry_collector(invoice: Any, now: datetime) -> None:
    """The dunning retry (#399): one more collection job for the new attempt number."""
    enqueue_collect(invoice)


def process_event(*, event: UUID, **payload: object) -> None:
    """``billing.process_event`` (global): fetch the payment a verified webhook named and apply it."""
    from skeleton.tenancy.context import system_context

    from .models import ProviderEvent

    record = ProviderEvent.objects.select_related("provider_account").filter(pk=event).first()
    if record is None or record.status != ProviderEvent.Status.RECEIVED:
        return
    if not record.payment_reference:
        record.status, record.outcome = ProviderEvent.Status.IGNORED, "no_payment"
        record.save()
        return
    provider_account = record.provider_account
    provider = get_provider(provider_account.provider)
    status = provider.fetch_payment(provider_account, record.payment_reference)
    parsed = parse_invoice_reference(status.invoice_reference)
    if parsed is None:
        record.status, record.outcome = ProviderEvent.Status.IGNORED, "foreign_reference"
        record.save()
        return
    with system_context(reason="billing:webhook", organization_id=parsed[0]):
        outcome = apply_status(provider_account, status)
    record.status = ProviderEvent.Status.PROCESSED if outcome != "amount_mismatch" else ProviderEvent.Status.FAILED
    record.outcome = outcome[:64]
    record.save()


def reconcile(*, now: datetime | None = None) -> int:
    """Fetch the provider's answer for the old pending payments of the current organization."""
    from .models import Payment, ProviderAccount

    now = engine._now(now)
    pending = list(
        Payment.objects.filter(status="pending", created_at__lte=now - RECONCILE_AFTER)
        .exclude(provider="")
        .exclude(provider_reference="")
        .values_list("provider", "provider_reference")
    )
    done = 0
    for code, reference in pending:
        provider_account = ProviderAccount.objects.filter(provider=code, enabled=True).order_by("-is_default").first()
        if provider_account is None:
            continue
        try:
            status = get_provider(code).fetch_payment(provider_account, reference)
        except ProviderError:
            continue
        with transaction.atomic():
            apply_status(provider_account, status, now=now)
        done += 1
    return done


def reconcile_job(**payload: object) -> None:
    if engine.billing_enabled():
        reconcile()


def register_payment_jobs(*, enabled: bool) -> None:
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    from .dunning import register_collector

    register_job_type(
        JobType(COLLECT_JOB, collect_job, {"invoice": "uuid"}, run_as="system", max_attempts=5, owner_app="sk_billing")
    )
    register_job_type(
        JobType(
            PROCESS_JOB,
            process_event,
            {"event": "uuid"},
            run_as="system",
            max_attempts=8,
            scope="global",
            owner_app="sk_billing",
        )
    )
    register_job_type(
        JobType(
            RECONCILE_JOB, reconcile_job, {"window": "str"}, run_as="system", max_attempts=3, owner_app="sk_billing"
        )
    )
    register_collector(retry_collector)
    engine.register_after_issue(enqueue_collect)
    if enabled:
        register_periodic("billing-reconcile", RECONCILE_JOB, every=timedelta(minutes=30))
