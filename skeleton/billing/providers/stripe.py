"""Stripe adapter (#401): Checkout Sessions, off-session PaymentIntents and signed webhooks.

- **Customer:** ``POST /v1/customers`` (form encoded), reused by id.
- **Checkout:** ``POST /v1/checkout/sessions`` with ``mode=payment``, one inline line item, the
  invoice reference in ``client_reference_id`` and in the PaymentIntent metadata, and
  ``setup_future_usage=off_session`` so the card is saved for the next renewals.
- **Off-session charge:** ``POST /v1/payment_intents`` with ``off_session=true`` and
  ``confirm=true`` on the saved payment method. A decline (402) or ``requires_action`` is a
  failure: the person must pay by checkout.
- **Fetch:** ``GET /v1/payment_intents/<id>`` (a ``cs_`` reference is resolved through its
  Checkout Session first), and the payment method for brand and last digits.
- **Refund:** ``POST /v1/refunds``.
- **Webhook:** ``Stripe-Signature: t=<unix>,v1=<hex>``; HMAC-SHA256 of ``"<t>.<body>"`` with the
  endpoint secret, any ``v1`` may match (secret rotation), five minutes of tolerance. The event
  names the PaymentIntent (``payment_intent.*``) or the Checkout Session
  (``checkout.session.completed``).

Every POST carries ``Idempotency-Key``; ``Stripe-Version`` is fixed so a dashboard upgrade never
changes the answers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from . import http
from .base import (
    Capability,
    ChargeResult,
    CheckoutSession,
    CustomerData,
    InvoiceData,
    PaymentState,
    PaymentStatus,
    ProviderError,
    RefundResult,
    VerifiedNotification,
    WebhookRejected,
    register_provider,
)

BASE_URL = "https://api.stripe.com"
API_VERSION = "2024-06-20"
TOLERANCE_SECONDS = 300
_STATES = {
    "succeeded": PaymentState.SUCCEEDED,
    "canceled": PaymentState.CANCELED,
    "requires_payment_method": PaymentState.FAILED,
}


class StripeProvider:
    code = "stripe"
    label = "Stripe"

    def capabilities(self, account: Any) -> frozenset[Capability]:
        return frozenset({Capability.HOSTED_CHECKOUT, Capability.OFF_SESSION_CARD, Capability.CARD, Capability.REFUND})

    def currencies(self, account: Any) -> frozenset[str] | None:
        return None

    def _headers(self, account: Any, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {account.secret('api_key')}", "Stripe-Version": API_VERSION}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _call(
        self, account: Any, method: str, path: str, *, form: list[tuple[str, Any]] | None = None, key: str | None = None
    ) -> Any:
        status, data = http.call(method, f"{BASE_URL}{path}", headers=self._headers(account, key), form=form)
        if status >= 400:
            error = (data or {}).get("error", {}) if isinstance(data, dict) else {}
            raise StripeError(str(error.get("code") or error.get("type") or f"http_{status}"), data=data)
        return data

    def ensure_customer(self, account: Any, customer: CustomerData, *, existing_id: str = "") -> str:
        if existing_id:
            return existing_id
        form = [("metadata[organization]", str(customer.organization_id))]
        if customer.name:
            form.append(("name", customer.name))
        if customer.email:
            form.append(("email", customer.email))
        data = self._call(account, "POST", "/v1/customers", form=form, key=f"customer:{customer.organization_id}")
        return str(data["id"])

    def create_checkout(
        self, account: Any, invoice: InvoiceData, *, customer_id: str, return_url: str, idempotency_key: str
    ) -> CheckoutSession:
        form: list[tuple[str, Any]] = [
            ("mode", "payment"),
            ("customer", customer_id),
            ("client_reference_id", invoice.reference),
            ("success_url", return_url),
            ("cancel_url", return_url),
            ("line_items[0][quantity]", 1),
            ("line_items[0][price_data][currency]", invoice.currency.lower()),
            ("line_items[0][price_data][unit_amount]", invoice.amount),
            ("line_items[0][price_data][product_data][name]", invoice.description),
            ("payment_intent_data[setup_future_usage]", "off_session"),
            ("payment_intent_data[metadata][invoice_reference]", invoice.reference),
            ("metadata[invoice_reference]", invoice.reference),
        ]
        data = self._call(account, "POST", "/v1/checkout/sessions", form=form, key=idempotency_key)
        expires = data.get("expires_at")
        return CheckoutSession(
            reference=str(data["id"]),
            url=str(data["url"]),
            expires_at=datetime.fromtimestamp(int(expires), UTC) if expires else None,
        )

    def charge_saved_method(
        self, account: Any, invoice: InvoiceData, *, customer_id: str, method_ref: str, idempotency_key: str
    ) -> ChargeResult:
        form: list[tuple[str, Any]] = [
            ("amount", invoice.amount),
            ("currency", invoice.currency.lower()),
            ("customer", customer_id),
            ("payment_method", method_ref),
            ("off_session", "true"),
            ("confirm", "true"),
            ("description", invoice.description),
            ("metadata[invoice_reference]", invoice.reference),
        ]
        try:
            data = self._call(account, "POST", "/v1/payment_intents", form=form, key=idempotency_key)
        except StripeError as exc:
            intent = ((exc.data or {}).get("error") or {}).get("payment_intent") if isinstance(exc.data, dict) else None
            if intent and intent.get("id"):
                return ChargeResult(reference=str(intent["id"]), status=PaymentState.FAILED, failure_code=exc.code)
            raise
        return ChargeResult(reference=str(data["id"]), status=_state(data))

    def fetch_payment(self, account: Any, reference: str) -> PaymentStatus:
        if reference.startswith("cs_"):
            session = self._call(account, "GET", f"/v1/checkout/sessions/{quote(reference)}")
            intent_id = session.get("payment_intent")
            if not intent_id:
                return PaymentStatus(
                    reference=reference,
                    status=PaymentState.PENDING,
                    amount=int(session.get("amount_total") or 0),
                    currency=str(session.get("currency") or "").upper(),
                    invoice_reference=str(session.get("client_reference_id") or ""),
                    customer_id=str(session.get("customer") or ""),
                )
            reference = str(intent_id)
        data = self._call(account, "GET", f"/v1/payment_intents/{quote(reference)}")
        method_id = str(data.get("payment_method") or "")
        label = ""
        if method_id and data.get("status") == "succeeded":
            method = self._call(account, "GET", f"/v1/payment_methods/{quote(method_id)}")
            card = method.get("card") or {}
            if card:
                label = f"{str(card.get('brand', '')).title()} •••• {card.get('last4', '')}"
        error = data.get("last_payment_error") or {}
        return PaymentStatus(
            reference=str(data["id"]),
            status=_state(data),
            amount=int(data.get("amount_received") or data.get("amount") or 0),
            currency=str(data.get("currency") or "").upper(),
            invoice_reference=str((data.get("metadata") or {}).get("invoice_reference") or ""),
            method="card",
            method_ref=method_id if data.get("status") == "succeeded" else "",
            method_label=label,
            customer_id=str(data.get("customer") or ""),
            failure_code=str(error.get("code") or error.get("decline_code") or ""),
        )

    def refund(self, account: Any, reference: str, *, amount: int, idempotency_key: str) -> RefundResult:
        data = self._call(
            account,
            "POST",
            "/v1/refunds",
            form=[("payment_intent", reference), ("amount", amount)],
            key=idempotency_key,
        )
        status = PaymentState.REFUNDED if data.get("status") == "succeeded" else PaymentState.PENDING
        return RefundResult(reference=str(data["id"]), status=status)

    def verify_webhook(
        self,
        account: Any,
        headers: Mapping[str, str],
        body: bytes,
        *,
        now: datetime,
        query: Mapping[str, str] | None = None,
    ) -> VerifiedNotification:
        timestamp, signatures = _parse_signature(headers.get("stripe-signature", ""))
        if timestamp is None or not signatures:
            raise WebhookRejected("missing_signature")
        if abs(now.timestamp() - timestamp) > TOLERANCE_SECONDS:
            raise WebhookRejected("stale_timestamp")
        secret = account.secret("webhook_secret").encode()
        expected = hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
            raise WebhookRejected("invalid_signature")
        try:
            event = json.loads(body)
            obj = event["data"]["object"]
        except (ValueError, KeyError, TypeError):
            raise WebhookRejected("invalid_body") from None
        kind = str(event.get("type") or "")
        reference = None
        if kind.startswith("payment_intent."):
            reference = str(obj.get("id") or "") or None
        elif kind.startswith("checkout.session."):
            reference = str(obj.get("payment_intent") or obj.get("id") or "") or None
        return VerifiedNotification(event_id=str(event.get("id") or ""), payment_reference=reference, kind=kind)


class StripeError(ProviderError):
    def __init__(self, code: str, *, data: Any = None):
        super().__init__(code)
        self.data = data


def _state(data: Mapping[str, Any]) -> PaymentState:
    return _STATES.get(str(data.get("status") or ""), PaymentState.PENDING)


def _parse_signature(header: str) -> tuple[int | None, list[str]]:
    timestamp = None
    signatures = []
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name == "t" and value.isdigit():
            timestamp = int(value)
        elif name == "v1" and value:
            signatures.append(value)
    return timestamp, signatures


PROVIDER = StripeProvider()


def register() -> None:
    register_provider(PROVIDER)
