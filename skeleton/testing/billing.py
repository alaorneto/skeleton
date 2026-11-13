"""A deterministic payment provider for tests (#400): no network, every outcome under control.

``FakeProvider`` keeps its payments in memory. ``create_checkout`` opens a session whose payment
the test completes with ``pay(reference)`` (or fails with ``fail``); ``charge_saved_method``
answers ``next_charge`` (succeeded by default). Webhooks are signed with HMAC-SHA256 of the body
in ``X-Fake-Signature`` with the account's webhook secret, and ``webhook(...)`` builds one.

Register it with ``use_fake_provider()`` (a context manager that also creates the
``ProviderAccount``), never in production settings.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from skeleton.billing.providers import base
from skeleton.billing.providers.base import (
    Capability,
    ChargeResult,
    CheckoutSession,
    CustomerData,
    InvoiceData,
    PaymentState,
    PaymentStatus,
    RefundResult,
    VerifiedNotification,
    WebhookRejected,
)

SECRET = "whsec_fake_billing_tests"  # noqa: S105 - fixed secret of the in-memory test provider


@dataclass
class FakeProvider:
    code: str = "fake"
    label: str = "Fake"
    payments: dict[str, PaymentStatus] = field(default_factory=dict)
    customers: dict[str, CustomerData] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    next_charge: PaymentState = PaymentState.SUCCEEDED
    supported: frozenset[Capability] = frozenset(Capability)

    def capabilities(self, account: Any) -> frozenset[Capability]:
        return self.supported

    def currencies(self, account: Any) -> frozenset[str] | None:
        return None

    def ensure_customer(self, account: Any, customer: CustomerData, *, existing_id: str = "") -> str:
        customer_id = existing_id or f"cus_{customer.organization_id}"
        self.customers[customer_id] = customer
        self.calls.append(("ensure_customer", customer_id))
        return customer_id

    def create_checkout(
        self, account: Any, invoice: InvoiceData, *, customer_id: str, return_url: str, idempotency_key: str
    ) -> CheckoutSession:
        reference = f"pay_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"
        self.payments.setdefault(
            reference,
            PaymentStatus(
                reference=reference,
                status=PaymentState.PENDING,
                amount=invoice.amount,
                currency=invoice.currency,
                invoice_reference=invoice.reference,
                customer_id=customer_id,
            ),
        )
        self.calls.append(("create_checkout", idempotency_key))
        return CheckoutSession(reference=reference, url=f"https://fake.example.test/pay/{reference}")

    def charge_saved_method(
        self, account: Any, invoice: InvoiceData, *, customer_id: str, method_ref: str, idempotency_key: str
    ) -> ChargeResult:
        reference = f"ch_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"
        self.calls.append(("charge", idempotency_key))
        if reference not in self.payments:
            self.payments[reference] = PaymentStatus(
                reference=reference,
                status=self.next_charge,
                amount=invoice.amount,
                currency=invoice.currency,
                invoice_reference=invoice.reference,
                method_ref=method_ref,
                customer_id=customer_id,
                failure_code="card_declined" if self.next_charge == PaymentState.FAILED else "",
            )
        return ChargeResult(reference=reference, status=self.payments[reference].status)

    def fetch_payment(self, account: Any, reference: str) -> PaymentStatus:
        self.calls.append(("fetch", reference))
        return self.payments[reference]

    def refund(self, account: Any, reference: str, *, amount: int, idempotency_key: str) -> RefundResult:
        self.payments[reference] = replace(self.payments[reference], status=PaymentState.REFUNDED)
        return RefundResult(reference=reference, status=PaymentState.REFUNDED)

    def verify_webhook(
        self,
        account: Any,
        headers: Mapping[str, str],
        body: bytes,
        *,
        now: datetime,
        query: Mapping[str, str] | None = None,
    ) -> VerifiedNotification:
        secret = account.secret("webhook_secret")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, headers.get("x-fake-signature", "")):
            raise WebhookRejected("invalid_signature")
        data = json.loads(body)
        return VerifiedNotification(event_id=str(data["id"]), payment_reference=data.get("payment"), kind="payment")

    # Test controls ------------------------------------------------------------------------------

    def pay(self, reference: str, *, amount: int | None = None, method_ref: str = "pm_fake_4242") -> None:
        current = self.payments[reference]
        self.payments[reference] = replace(
            current,
            status=PaymentState.SUCCEEDED,
            amount=current.amount if amount is None else amount,
            method_ref=method_ref,
            method_label="Visa •••• 4242",
        )

    def fail(self, reference: str) -> None:
        self.payments[reference] = replace(
            self.payments[reference], status=PaymentState.FAILED, failure_code="declined"
        )

    @staticmethod
    def webhook(payment: str | None, *, event_id: str | None = None, secret: str = SECRET) -> tuple[bytes, str]:
        body = json.dumps({"id": event_id or f"evt_{uuid4().hex}", "payment": payment}).encode()
        return body, hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@contextmanager
def use_fake_provider(*, currencies: tuple[str, ...] = ()) -> Iterator[tuple[FakeProvider, Any]]:
    """Register a fresh ``FakeProvider`` and its enabled default ``ProviderAccount`` for the block."""
    from skeleton.billing.models import ProviderAccount
    from skeleton.tenancy.context import system_context

    provider = FakeProvider()
    previous = base._providers.get(provider.code)
    base._providers[provider.code] = provider
    with system_context(reason="test:fake-provider"):
        account = ProviderAccount(
            provider=provider.code,
            label="Fake",
            mode=ProviderAccount.Mode.TEST,
            currencies=list(currencies),
            is_default=True,
        )
        account.set_secrets(api_key="sk_fake", webhook_secret=SECRET)
        account.save()
    try:
        yield provider, account
    finally:
        if previous is None:
            base._providers.pop(provider.code, None)
        else:
            base._providers[provider.code] = previous
