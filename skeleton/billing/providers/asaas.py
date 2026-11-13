"""Asaas adapter (#403): BRL charges with a hosted invoice page and tokenized cards.

- **Customer:** found by ``externalReference`` (the organization) or created with ``cpfCnpj``,
  which Asaas requires.
- **Checkout:** ``POST /v3/payments`` with ``billingType=UNDEFINED`` (the person picks Pix,
  boleto or card on ``invoiceUrl``) and ``externalReference`` = the invoice reference. Asaas has
  no idempotency header: an open charge with the same reference is looked up first and reused,
  so a retry never creates a second charge.
- **Off-session charge:** ``billingType=CREDIT_CARD`` with the ``creditCardToken`` returned by a
  previous card payment.
- **Fetch:** ``GET /v3/payments/<id>``: ``RECEIVED``, ``CONFIRMED`` and ``RECEIVED_IN_CASH`` pay;
  ``REFUNDED`` refunds; ``OVERDUE`` stays pending (the core's dunning decides); the rest is pending.
- **Refund:** ``POST /v3/payments/<id>/refund``.
- **Webhook:** Asaas sends the token configured for the webhook in ``asaas-access-token``,
  without HMAC. It is compared in constant time, and, as with every provider, the payment is
  always fetched back before anything changes, so a forged body has no effect.

BRL only. The sandbox and production base URLs are fixed by the account's mode.
"""

from __future__ import annotations

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

PRODUCTION_URL = "https://api.asaas.com/v3"
SANDBOX_URL = "https://api-sandbox.asaas.com/v3"
_PAID = {"RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"}
_METHODS = {"PIX": "pix", "BOLETO": "boleto", "CREDIT_CARD": "card"}


def _decimal(amount: int) -> float:
    return float(Decimal(money.format_amount(amount, "BRL")))


def _minor(value: Any) -> int:
    try:
        return money.parse(str(Decimal(str(value)).quantize(Decimal("0.01"))), "BRL")
    except Exception:  # noqa: BLE001 - a malformed amount never matches an invoice
        return -1


class AsaasProvider:
    code = "asaas"
    label = "Asaas"

    def capabilities(self, account: Any) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.HOSTED_CHECKOUT,
                Capability.OFF_SESSION_CARD,
                Capability.CARD,
                Capability.PIX,
                Capability.BOLETO,
                Capability.REFUND,
            }
        )

    def currencies(self, account: Any) -> frozenset[str] | None:
        return frozenset({"BRL"})

    def _base(self, account: Any) -> str:
        return SANDBOX_URL if account.mode == "test" else PRODUCTION_URL

    def _call(self, account: Any, method: str, path: str, *, body: Any = None) -> Any:
        headers = {"access_token": account.secret("api_key"), "User-Agent": "skeleton-billing"}
        status, data = http.call(method, f"{self._base(account)}{path}", headers=headers, json_body=body)
        if status >= 400:
            errors = (data or {}).get("errors") if isinstance(data, dict) else None
            code = errors[0].get("code") if errors else None
            raise ProviderError(str(code or f"http_{status}"))
        return data

    def ensure_customer(self, account: Any, customer: CustomerData, *, existing_id: str = "") -> str:
        if existing_id:
            return existing_id
        reference = str(customer.organization_id)
        found = self._call(account, "GET", f"/customers?externalReference={quote(reference)}")
        if (found or {}).get("data"):
            return str(found["data"][0]["id"])
        digits = "".join(filter(str.isdigit, customer.tax_id))
        if not digits:
            raise ProviderError("tax_id_required")
        body = {"name": customer.name, "cpfCnpj": digits, "externalReference": reference}
        if customer.email:
            body["email"] = customer.email
        return str(self._call(account, "POST", "/customers", body=body)["id"])

    def _open_charge(self, account: Any, reference: str) -> Any:
        found = self._call(account, "GET", f"/payments?externalReference={quote(reference)}&status=PENDING")
        rows = (found or {}).get("data") or []
        return rows[0] if rows else None

    def create_checkout(
        self, account: Any, invoice: InvoiceData, *, customer_id: str, return_url: str, idempotency_key: str
    ) -> CheckoutSession:
        if invoice.currency != "BRL":
            raise ProviderError("currency_not_supported")
        existing = self._open_charge(account, invoice.reference)
        data = existing or self._call(
            account,
            "POST",
            "/payments",
            body={
                "customer": customer_id,
                "billingType": "UNDEFINED",
                "value": _decimal(invoice.amount),
                "dueDate": invoice.due_at.date().isoformat(),
                "description": invoice.description,
                "externalReference": invoice.reference,
            },
        )
        return CheckoutSession(reference=str(data["id"]), url=str(data["invoiceUrl"]))

    def charge_saved_method(
        self, account: Any, invoice: InvoiceData, *, customer_id: str, method_ref: str, idempotency_key: str
    ) -> ChargeResult:
        if invoice.currency != "BRL":
            raise ProviderError("currency_not_supported")
        existing = self._open_charge(account, invoice.reference)
        if existing is not None and existing.get("billingType") == "CREDIT_CARD":
            return ChargeResult(reference=str(existing["id"]), status=PaymentState.PENDING)
        data = self._call(
            account,
            "POST",
            "/payments",
            body={
                "customer": customer_id,
                "billingType": "CREDIT_CARD",
                "value": _decimal(invoice.amount),
                "dueDate": invoice.due_at.date().isoformat(),
                "description": invoice.description,
                "externalReference": invoice.reference,
                "creditCardToken": method_ref,
            },
        )
        status = PaymentState.SUCCEEDED if data.get("status") in _PAID else PaymentState.PENDING
        return ChargeResult(reference=str(data["id"]), status=status)

    def fetch_payment(self, account: Any, reference: str) -> PaymentStatus:
        data = self._call(account, "GET", f"/payments/{quote(reference)}")
        raw = str(data.get("status") or "")
        if raw in _PAID:
            state = PaymentState.SUCCEEDED
        elif raw in {"REFUNDED", "REFUND_REQUESTED", "CHARGEBACK_REQUESTED"}:
            state = PaymentState.REFUNDED
        else:
            state = PaymentState.PENDING
        card = data.get("creditCard") or {}
        token = str(card.get("creditCardToken") or "")
        number = str(card.get("creditCardNumber") or "")
        return PaymentStatus(
            reference=str(data["id"]),
            status=state,
            amount=_minor(data.get("value")),
            currency="BRL",
            invoice_reference=str(data.get("externalReference") or ""),
            method=_METHODS.get(str(data.get("billingType") or ""), "card"),
            method_ref=token if state == PaymentState.SUCCEEDED else "",
            method_label=f"{str(card.get('creditCardBrand', '')).title()} •••• {number[-4:]}" if number else "",
            customer_id=str(data.get("customer") or ""),
        )

    def refund(self, account: Any, reference: str, *, amount: int, idempotency_key: str) -> RefundResult:
        data = self._call(account, "POST", f"/payments/{quote(reference)}/refund", body={"value": _decimal(amount)})
        status = PaymentState.REFUNDED if data.get("status") == "REFUNDED" else PaymentState.PENDING
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
        expected = account.secret("webhook_secret")
        received = headers.get("asaas-access-token", "")
        if not expected or not hmac.compare_digest(expected.encode(), received.encode()):
            raise WebhookRejected("invalid_token")
        try:
            event = json.loads(body)
        except ValueError:
            raise WebhookRejected("invalid_body") from None
        payment = event.get("payment") or {}
        return VerifiedNotification(
            event_id=str(event.get("id") or f"{event.get('event')}:{payment.get('id')}"),
            payment_reference=str(payment.get("id") or "") or None,
            kind=str(event.get("event") or ""),
        )


PROVIDER = AsaasProvider()


def register() -> None:
    register_provider(PROVIDER)
