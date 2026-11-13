"""Platform operations of billing (#405): the catalog, vouchers, provider accounts and accounts.

The catalog (products, prices, vouchers, provider accounts) is global: the platform
administrator and the limited ``billing`` platform role (capability ``billing.operate``)
manage it. Acting on one organization's account (offline payment, voiding an invoice, an
access extension, a courtesy plan) opens that organization's ``system_context`` with the
``platform:billing`` reason, which only the platform administrator may open. Every action on an
account takes a reason, kept with the record it changed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext

from . import engine

PLATFORM_REASON = "platform:billing"
from skeleton.accounts.platform import BILLING_OPERATE  # noqa: E402


def require_operator(user: Any) -> None:
    from skeleton.accounts.platform import require_platform_context

    require_platform_context(user, capability=BILLING_OPERATE, message="Platform billing required.")


def require_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValidationError({"reason": gettext("Informe o motivo.")})
    return reason[:500]


# Catalog --------------------------------------------------------------------------------------


def create_product(user: Any, *, code: str, kind: str, name: str, app_label: str = "", is_public: bool = True) -> Any:
    from .models import Product

    require_operator(user)
    product = Product(code=code.strip(), kind=kind, app_label=app_label.strip(), name=name.strip(), is_public=is_public)
    product.save()
    return product


def create_price(user: Any, product: Any, **terms: Any) -> Any:
    from .models import Price

    require_operator(user)
    price = Price(product=product, **terms)
    price.save()
    return price


def set_price_flags(user: Any, price: Any, *, is_active: bool, is_default: bool, expected_revision: int) -> Any:
    from skeleton.common.exceptions import ConcurrentUpdate

    from .models import Price

    require_operator(user)
    locked = Price.objects.select_for_update().get(pk=price.pk)
    if locked.revision != expected_revision:
        raise ConcurrentUpdate("The price changed.")
    if is_default:
        for other in (
            Price.objects.select_for_update().filter(currency=locked.currency, is_default=True).exclude(pk=locked.pk)
        ):
            other.is_default = False
            other.save(update_fields={"is_default"})
    locked.is_active, locked.is_default = is_active, is_default
    locked.save(update_fields={"is_active", "is_default"})
    return locked


def create_voucher(user: Any, **terms: Any) -> Any:
    from .models import Voucher

    require_operator(user)
    voucher = Voucher(**terms)
    voucher.save()
    return voucher


def set_voucher_active(user: Any, voucher: Any, *, active: bool, expected_revision: int) -> Any:
    from skeleton.common.exceptions import ConcurrentUpdate

    from .models import Voucher

    require_operator(user)
    locked = Voucher.objects.select_for_update().get(pk=voucher.pk)
    if locked.revision != expected_revision:
        raise ConcurrentUpdate("The voucher changed.")
    locked.is_active = active
    locked.save(update_fields={"is_active"})
    return locked


def save_provider_account(
    user: Any,
    account: Any = None,
    *,
    provider: str,
    label: str,
    mode: str,
    currencies: list[str],
    enabled: bool,
    is_default: bool,
    api_key: str = "",
    webhook_secret: str = "",
    expected_revision: int | None = None,
) -> Any:
    """Create or update a provider account; an empty secret keeps the stored one."""
    from skeleton.common.exceptions import ConcurrentUpdate

    from .models import ProviderAccount
    from .providers.base import _providers

    require_operator(user)
    if provider not in _providers:
        raise ValidationError({"provider": gettext("Provedor desconhecido.")})
    if account is None:
        account = ProviderAccount(provider=provider)
        if not api_key or not webhook_secret:
            raise ValidationError({"api_key": gettext("Informe a chave e o segredo do webhook.")})
    else:
        account = ProviderAccount.objects.select_for_update().get(pk=account.pk)
        if expected_revision is not None and account.revision != expected_revision:
            raise ConcurrentUpdate("The provider account changed.")
    if is_default:
        for other in ProviderAccount.objects.select_for_update().filter(is_default=True).exclude(pk=account.pk):
            other.is_default = False
            other.save(update_fields={"is_default"})
    account.label, account.mode, account.currencies = label.strip(), mode, currencies
    account.enabled, account.is_default = enabled, is_default
    account.set_secrets(api_key=api_key, webhook_secret=webhook_secret)
    account.save()
    return account


# One organization's account ---------------------------------------------------------------------


def _in_organization(user: Any, organization_id: Any) -> Any:
    from skeleton.accounts.platform import is_platform_admin
    from skeleton.tenancy.context import system_context

    if not is_platform_admin(user):
        raise PermissionDenied("Acting on an organization's billing needs the platform administrator.")
    return system_context(reason=PLATFORM_REASON, organization_id=organization_id)


def account_snapshot(user: Any, organization_id: Any) -> dict[str, Any]:
    """What the platform page shows of one organization's billing (read only)."""
    from .models import Invoice, Payment

    with _in_organization(user, organization_id):
        subscription = engine.current_subscription()
        return {
            "account": engine.account(),
            "subscription": subscription,
            "items": list(engine.live_items(subscription, now=engine.clock(), statuses=("active", "pending")))
            if subscription
            else [],
            "invoices": list(Invoice.objects.order_by("-number")[:50]),
            "payments": list(Payment.objects.select_related("invoice").order_by("-created_at")[:50]),
        }


def mark_paid_offline(
    user: Any, organization_id: Any, invoice_id: Any, *, reason: str, now: datetime | None = None
) -> Any:
    """Register a payment made outside the providers (a transfer, a negotiation) and settle the invoice."""
    from .models import Invoice, Payment

    note = require_reason(reason)
    with _in_organization(user, organization_id):
        invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
        owed = invoice.total - invoice.amount_paid
        if invoice.status not in (Invoice.Status.OPEN, Invoice.Status.UNCOLLECTIBLE) or owed <= 0:
            raise ValidationError({"invoice": gettext("Esta fatura não tem valor em aberto.")})
        Payment(
            organization_id=organization_id,
            invoice=invoice,
            method=Payment.Method.OFFLINE,
            status=Payment.Status.SUCCEEDED,
            currency=invoice.currency,
            amount=owed,
            note=note,
            succeeded_at=engine._now(now),
        ).save()
        return engine.settle_invoice(invoice, now=now)


def void(user: Any, organization_id: Any, invoice_id: Any, *, reason: str, now: datetime | None = None) -> Any:
    from .models import Invoice

    note = require_reason(reason)
    with _in_organization(user, organization_id):
        invoice = engine.void_invoice(Invoice.objects.get(pk=invoice_id), now=now)
        invoice.status_note = note
        invoice.save(update_fields={"status_note"})
        from .dunning import refresh_access

        refresh_access(now=now)
        return invoice


def extend_access(
    user: Any, organization_id: Any, *, until: datetime | None, reason: str, now: datetime | None = None
) -> Any:
    """Keep (or stop keeping, with ``until=None``) the organization active whatever it owes."""
    from .dunning import refresh_access

    note = require_reason(reason)
    with _in_organization(user, organization_id):
        account = engine.ensure_account()
        account = engine.account(lock=True)
        account.access_override_until = until
        account.access_override_reason = note if until else ""
        account.save(update_fields={"access_override_until", "access_override_reason"})
        refresh_access(now=now)
        return account


def grant_courtesy(user: Any, organization_id: Any, price: Any, *, reason: str, now: datetime | None = None) -> Any:
    """A plan at no cost: the price is contracted and a private 100% voucher covers every renewal."""
    from .models import Voucher, VoucherRedemption

    note = require_reason(reason)
    now = engine._now(now)
    with _in_organization(user, organization_id):
        if engine.current_subscription() is None:
            engine.start_subscription(price, now=now)
        elif price.product.kind == "base_plan":
            engine.change_plan(price, now=now)
        else:
            engine.add_item(price, now=now)
        code = f"COURTESY-{str(organization_id)[:8].upper()}-{now.strftime('%Y%m%d%H%M%S')}"
        voucher = Voucher(
            code=code, kind=Voucher.Kind.PERCENT, value=10000, duration=Voucher.Duration.FOREVER, max_redemptions=1
        )
        voucher.save()
        VoucherRedemption(
            organization_id=organization_id,
            voucher=voucher,
            subscription=engine.current_subscription(),
            redeemed_at=now,
            remaining_cycles=None,
        ).save()
        voucher.redemption_count = 1
        voucher.save(update_fields={"redemption_count"})
        from .models import Invoice

        for invoice in Invoice.objects.filter(status=Invoice.Status.OPEN, issued_at=now):
            mark_note = f"Cortesia: {note}"[:500]
            _settle_courtesy(invoice, note=mark_note, now=now)
        return engine.current_subscription()


def _settle_courtesy(invoice: Any, *, note: str, now: datetime) -> None:
    from .models import Payment

    owed = invoice.total - invoice.amount_paid
    if owed > 0:
        Payment(
            organization_id=invoice.organization_id,
            invoice=invoice,
            method=Payment.Method.OFFLINE,
            status=Payment.Status.SUCCEEDED,
            currency=invoice.currency,
            amount=owed,
            note=note,
            succeeded_at=now,
        ).save()
    engine.settle_invoice(invoice, now=now)
