"""The payment provider protocol (#400).

There is no open standard that abstracts subscriptions across providers, so the core keeps the
subscription and asks a provider only for what all of them do:

- create or find the **customer**;
- open a **hosted checkout** for one invoice (no card data ever reaches the server: PCI SAQ A);
- **charge a saved method** off session, when the provider supports it;
- **fetch a payment**: the authoritative status, used after every webhook (a webhook is only a
  ping and its body is never trusted) and by the reconciliation;
- **refund**;
- **verify a webhook** (signature, timestamp tolerance) and say which payment it is about.

Every call that creates something takes an idempotency key. The reference sent to the provider
is ``<organization sysid>:<invoice sysid>`` (``invoice_reference``): the payment fetched back
names its organization and invoice without trusting the webhook body.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


class Capability(StrEnum):
    HOSTED_CHECKOUT = "hosted_checkout"
    OFF_SESSION_CARD = "off_session_card"
    PIX = "pix"
    BOLETO = "boleto"
    CARD = "card"
    REFUND = "refund"


class PaymentState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELED = "canceled"


@dataclass(frozen=True)
class CustomerData:
    organization_id: UUID
    name: str
    email: str
    tax_id: str
    country: str


@dataclass(frozen=True)
class InvoiceData:
    organization_id: UUID
    invoice_id: UUID
    number: str
    currency: str
    amount: int  # Minor units still owed.
    description: str
    due_at: datetime

    @property
    def reference(self) -> str:
        return invoice_reference(self.organization_id, self.invoice_id)


@dataclass(frozen=True)
class CheckoutSession:
    reference: str
    url: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class PaymentStatus:
    """What the provider says about one payment, fetched with the platform's own key."""

    reference: str
    status: PaymentState
    amount: int
    currency: str
    invoice_reference: str
    method: str = "card"  # card, pix or boleto.
    method_ref: str = ""  # A reusable token of the method, when the provider returned one.
    method_label: str = ""  # Brand and last digits only.
    customer_id: str = ""
    failure_code: str = ""


@dataclass(frozen=True)
class ChargeResult:
    reference: str
    status: PaymentState
    failure_code: str = ""


@dataclass(frozen=True)
class RefundResult:
    reference: str
    status: PaymentState


@dataclass(frozen=True)
class VerifiedNotification:
    event_id: str
    payment_reference: str | None
    kind: str = ""


class ProviderError(Exception):
    """A provider call failed; ``retryable`` says whether the same call may be tried again."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class WebhookRejected(Exception):
    """The webhook is not authentic (signature, token or timestamp); answer without effect."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def invoice_reference(organization_id: Any, invoice_id: Any) -> str:
    return f"{organization_id}:{invoice_id}"


def parse_invoice_reference(reference: str) -> tuple[UUID, UUID] | None:
    try:
        organization, invoice = str(reference).split(":", 1)
        return UUID(organization), UUID(invoice)
    except (ValueError, AttributeError):
        return None


class PaymentProvider(Protocol):
    code: str
    label: str

    def capabilities(self, account: Any) -> frozenset[Capability]: ...

    def currencies(self, account: Any) -> frozenset[str] | None:
        """Currencies the provider accepts; ``None`` for any."""

    def ensure_customer(self, account: Any, customer: CustomerData, *, existing_id: str = "") -> str: ...

    def create_checkout(
        self, account: Any, invoice: InvoiceData, *, customer_id: str, return_url: str, idempotency_key: str
    ) -> CheckoutSession: ...

    def charge_saved_method(
        self, account: Any, invoice: InvoiceData, *, customer_id: str, method_ref: str, idempotency_key: str
    ) -> ChargeResult: ...

    def fetch_payment(self, account: Any, reference: str) -> PaymentStatus: ...

    def refund(self, account: Any, reference: str, *, amount: int, idempotency_key: str) -> RefundResult: ...

    def verify_webhook(
        self,
        account: Any,
        headers: Mapping[str, str],
        body: bytes,
        *,
        now: datetime,
        query: Mapping[str, str] | None = None,
    ) -> VerifiedNotification:
        """Verify an inbound webhook; ``query`` is its query string (Mercado Pago signs ``data.id`` there)."""


_providers: dict[str, PaymentProvider] = {}


def register_provider(provider: PaymentProvider) -> None:
    existing = _providers.get(provider.code)
    if existing is not None and existing is not provider:
        raise ValueError(f"Payment provider {provider.code} is already registered.")
    _providers[provider.code] = provider


def get_provider(code: str) -> PaymentProvider:
    return _providers[code]


def all_providers() -> tuple[PaymentProvider, ...]:
    return tuple(_providers[code] for code in sorted(_providers))
