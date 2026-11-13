"""Mercado Pago adapter (#402): Checkout Pro (card, Pix and boleto) and signed notifications.

- **Customer:** found by email (``GET /v1/customers/search``) or created (``POST /v1/customers``).
- **Checkout:** ``POST /checkout/preferences`` with one item, the invoice reference as
  ``external_reference`` and ``back_urls``; the person pays by card, Pix or boleto on Mercado
  Pago's page (``init_point``; ``sandbox_init_point`` in test mode). Amounts are decimals here.
- **Off-session charge:** not offered. A saved card at Mercado Pago needs its security code
  for each payment outside the provider's own subscriptions (preapproval), which the core does
  not use; every invoice goes by checkout, Pix or boleto, so the capability is absent.
- **Fetch:** ``GET /v1/payments/<id>``: ``approved`` pays, ``rejected``/``cancelled`` fail,
  ``refunded``/``charged_back`` refund, the rest is pending. The notification only names the id.
- **Refund:** ``POST /v1/payments/<id>/refunds``.
- **Webhook:** ``x-signature: ts=<unix>,v1=<hex>``, HMAC-SHA256 with the webhook secret of the
  manifest ``id:<data.id>;request-id:<x-request-id>;ts:<ts>;`` (``data.id`` from the query
  string, lower case, else from the body), five minutes of tolerance.

POSTs carry ``X-Idempotency-Key``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from .. import money
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

BASE_URL = "https://api.mercadopago.com"
TOLERANCE_SECONDS = 300
_STATES = {
    "approved": PaymentState.SUCCEEDED,
    "rejected": PaymentState.FAILED,
    "cancelled": PaymentState.CANCELED,
    "refunded": PaymentState.REFUNDED,
    "charged_back": PaymentState.REFUNDED,
}
_METHODS = {"bank_transfer": "pix", "ticket": "boleto", "credit_card": "card", "debit_card": "card"}
CURRENCIES = frozenset({"ARS", "BRL", "CLP", "COP", "MXN", "PEN", "UYU"})


def _decimal(amount: int, currency: str) -> float:
    return float(Decimal(money.format_amount(amount, currency)))


def _minor(value: Any, currency: str) -> int:
    try:
        return money.parse(str(Decimal(str(value)).quantize(Decimal(1).scaleb(-money.exponent(currency)))), currency)
    except Exception:  # noqa: BLE001 - a malformed amount never matches an invoice
        return -1


class MercadoPagoProvider:
    code = "mercadopago"
    label = "Mercado Pago"

    def capabilities(self, account: Any) -> frozenset[Capability]:
        return frozenset(
            {Capability.HOSTED_CHECKOUT, Capability.CARD, Capability.PIX, Capability.BOLETO, Capability.REFUND}
        )

    def currencies(self, account: Any) -> frozenset[str] | None:
        return CURRENCIES

    def _headers(self, account: Any, key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {account.secret('api_key')}"}
        if key:
            headers["X-Idempotency-Key"] = key
        return headers

    def _call(self, account: Any, method: str, path: str, *, body: Any = None, key: str | None = None) -> Any:
        status, data = http.call(method, f"{BASE_URL}{path}", headers=self._headers(account, key), json_body=body)
        if status >= 400:
            detail = data if isinstance(data, dict) else {}
            raise ProviderError(str(detail.get("error") or detail.get("message") or f"http_{status}"))
        return data

    def ensure_customer(self, account: Any, customer: CustomerData, *, existing_id: str = "") -> str:
        if existing_id or not customer.email:
            return existing_id
        found = self._call(account, "GET", f"/v1/customers/search?email={quote(customer.email)}")
        results = (found or {}).get("results") or []
        if results:
            return str(results[0]["id"])
        body: dict[str, Any] = {"email": customer.email}
        if customer.tax_id:
            digits = "".join(filter(str.isdigit, customer.tax_id))
            body["identification"] = {"type": "CNPJ" if len(digits) > 11 else "CPF", "number": digits}
        data = self._call(account, "POST", "/v1/customers", body=body, key=f"customer:{customer.organization_id}")
        return str(data["id"])

    def create_checkout(
        self, account: Any, invoice: InvoiceData, *, customer_id: str, return_url: str, idempotency_key: str
    ) -> CheckoutSession:
        body = {
            "items": [
                {
                    "id": invoice.number,
                    "title": invoice.description,
                    "quantity": 1,
                    "currency_id": invoice.currency,
                    "unit_price": _decimal(invoice.amount, invoice.currency),
                }
            ],
            "external_reference": invoice.reference,
            "back_urls": {"success": return_url, "pending": return_url, "failure": return_url},
            "auto_return": "approved",
            "metadata": {"invoice_reference": invoice.reference},
        }
        data = self._call(account, "POST", "/checkout/preferences", body=body, key=idempotency_key)
        url = data.get("sandbox_init_point") if account.mode == "test" else data.get("init_point")
        return CheckoutSession(reference=str(data["id"]), url=str(url or data.get("init_point")))

    def charge_saved_method(
        self, account: Any, invoice: InvoiceData, *, customer_id: str, method_ref: str, idempotency_key: str
    ) -> ChargeResult:
        raise ProviderError("unsupported")

    def fetch_payment(self, account: Any, reference: str) -> PaymentStatus:
        data = self._call(account, "GET", f"/v1/payments/{quote(reference)}")
        currency = str(data.get("currency_id") or "")
        card = data.get("card") or {}
        last_four = str(card.get("last_four_digits") or "")
        return PaymentStatus(
            reference=str(data["id"]),
            status=_STATES.get(str(data.get("status") or ""), PaymentState.PENDING),
            amount=_minor(data.get("transaction_amount"), currency) if currency in money.CURRENCY_EXPONENTS else -1,
            currency=currency,
            invoice_reference=str(data.get("external_reference") or ""),
            method=_METHODS.get(str(data.get("payment_type_id") or ""), "card"),
            method_label=f"{str(data.get('payment_method_id', '')).title()} •••• {last_four}" if last_four else "",
            customer_id=str((data.get("payer") or {}).get("id") or ""),
            failure_code=str(data.get("status_detail") or "") if data.get("status") == "rejected" else "",
        )

    def refund(self, account: Any, reference: str, *, amount: int, idempotency_key: str) -> RefundResult:
        payment = self._call(account, "GET", f"/v1/payments/{quote(reference)}")
        currency = str(payment.get("currency_id") or "BRL")
        data = self._call(
            account,
            "POST",
            f"/v1/payments/{quote(reference)}/refunds",
            body={"amount": _decimal(amount, currency)},
            key=idempotency_key,
        )
        status = PaymentState.REFUNDED if data.get("status") == "approved" else PaymentState.PENDING
        return RefundResult(reference=str(data.get("id") or reference), status=status)

    def verify_webhook(
        self,
        account: Any,
        headers: Mapping[str, str],
        body: bytes,
        *,
        now: datetime,
        query: Mapping[str, str] | None = None,
    ) -> VerifiedNotification:
        parts = dict(part.strip().split("=", 1) for part in headers.get("x-signature", "").split(",") if "=" in part)
        timestamp, signature = parts.get("ts", ""), parts.get("v1", "")
        if not timestamp.isdigit() or not signature:
            raise WebhookRejected("missing_signature")
        if abs(now.timestamp() - int(timestamp)) > TOLERANCE_SECONDS:
            raise WebhookRejected("stale_timestamp")
        try:
            event = json.loads(body) if body else {}
        except ValueError:
            raise WebhookRejected("invalid_body") from None
        data_id = str((query or {}).get("data.id") or ((event.get("data") or {}).get("id")) or "")
        if data_id.isalnum():
            data_id = data_id.lower()
        manifest = f"id:{data_id};request-id:{headers.get('x-request-id', '')};ts:{timestamp};"
        expected = hmac.new(account.secret("webhook_secret").encode(), manifest.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WebhookRejected("invalid_signature")
        kind = str(event.get("type") or (query or {}).get("type") or "")
        reference = data_id if kind == "payment" and data_id else None
        event_id = str(event.get("id") or headers.get("x-request-id") or f"{kind}:{data_id}:{timestamp}")
        return VerifiedNotification(event_id=event_id, payment_reference=reference, kind=kind)


PROVIDER = MercadoPagoProvider()


def register() -> None:
    register_provider(PROVIDER)
