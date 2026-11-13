"""Billing of the organization (#404): rights and quotas for apps, the account for its surfaces.

Apps read their organization's standing (``access_state``, ``entitlement``,
``storage_allowance``) and check their own plan limits (``check_quota``). The billing pages and
API of the core use the rest, which needs the ``billing.view`` or ``billing.manage`` capability.
The service stays usable while the organization's access is limited (``access_exempt``): it is
how the account is settled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError

from .base import ServiceAPI, service_method
from .dto import Page, as_utc, paginate
from .errors import Conflict, Forbidden, InvalidInput, NotFound

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser


@dataclass(frozen=True, slots=True)
class AccessState:
    level: str  # active, past_due, restricted or suspended.
    since: datetime | None


@dataclass(frozen=True, slots=True)
class StorageAllowance:
    quota_bytes: int | None  # None: unlimited.
    used_bytes: int
    overage: str  # block or bill.


@dataclass(frozen=True, slots=True)
class SubscriptionItemSummary:
    sysid: UUID
    product: str
    kind: str
    app_label: str
    quantity: int
    unit_amount: int
    status: str
    ends_at: datetime | None
    trial_end: datetime | None


@dataclass(frozen=True, slots=True)
class SubscriptionSummary:
    sysid: UUID
    status: str
    currency: str
    interval: str
    interval_count: int
    current_period_start: datetime
    current_period_end: datetime
    trial_end: datetime | None
    cancel_at_period_end: bool
    scheduled_price: UUID | None
    items: tuple[SubscriptionItemSummary, ...]


@dataclass(frozen=True, slots=True)
class InvoiceSummary:
    sysid: UUID
    number: str
    status: str
    currency: str
    total: int
    amount_paid: int
    issued_at: datetime
    due_at: datetime
    paid_at: datetime | None


@dataclass(frozen=True, slots=True)
class PriceOffer:
    sysid: UUID
    product: str
    product_code: str
    kind: str
    app_label: str
    currency: str
    unit_amount: int
    interval: str
    interval_count: int
    trial_days: int
    included_storage_bytes: int


@dataclass(frozen=True, slots=True)
class CheckoutLink:
    url: str


def _require(actor: AbstractBaseUser, capability: str) -> None:
    from skeleton.access.capabilities import has_capability

    if not has_capability(actor, capability):  # type: ignore[arg-type]
        raise NotFound()


def _view(actor: AbstractBaseUser) -> None:
    from skeleton.access.capabilities import BILLING_VIEW

    _require(actor, BILLING_VIEW)


def _manage(actor: AbstractBaseUser) -> None:
    from skeleton.access.capabilities import BILLING_MANAGE

    _require(actor, BILLING_MANAGE)


def _invalid(error: ValidationError) -> InvalidInput:
    return InvalidInput(error.message_dict if hasattr(error, "error_dict") else {"__all__": error.messages})


def _summary(subscription: object) -> SubscriptionSummary:
    from skeleton.billing import engine

    items = engine.live_items(subscription, now=engine.clock(), statuses=("active", "pending"))
    return SubscriptionSummary(
        sysid=subscription.pk,  # type: ignore[attr-defined]
        status=subscription.status,  # type: ignore[attr-defined]
        currency=subscription.currency,  # type: ignore[attr-defined]
        interval=subscription.interval,  # type: ignore[attr-defined]
        interval_count=subscription.interval_count,  # type: ignore[attr-defined]
        current_period_start=as_utc(subscription.current_period_start),  # type: ignore[attr-defined]
        current_period_end=as_utc(subscription.current_period_end),  # type: ignore[attr-defined]
        trial_end=as_utc(subscription.trial_end) if subscription.trial_end else None,  # type: ignore[attr-defined]
        cancel_at_period_end=subscription.cancel_at_period_end,  # type: ignore[attr-defined]
        scheduled_price=subscription.scheduled_price_id,  # type: ignore[attr-defined]
        items=tuple(
            SubscriptionItemSummary(
                sysid=item.pk,
                product=item.price.product.name,
                kind=item.price.product.kind,
                app_label=item.app_label,
                quantity=item.quantity,
                unit_amount=item.price.unit_amount,
                status=item.status,
                ends_at=as_utc(item.ends_at) if item.ends_at else None,
                trial_end=as_utc(item.trial_end) if item.trial_end else None,
            )
            for item in items
        ),
    )


def _invoice(invoice: object) -> InvoiceSummary:
    return InvoiceSummary(
        sysid=invoice.pk,  # type: ignore[attr-defined]
        number=invoice.number,  # type: ignore[attr-defined]
        status=invoice.status,  # type: ignore[attr-defined]
        currency=invoice.currency,  # type: ignore[attr-defined]
        total=invoice.total,  # type: ignore[attr-defined]
        amount_paid=invoice.amount_paid,  # type: ignore[attr-defined]
        issued_at=as_utc(invoice.issued_at),  # type: ignore[attr-defined]
        due_at=as_utc(invoice.due_at),  # type: ignore[attr-defined]
        paid_at=as_utc(invoice.paid_at) if invoice.paid_at else None,  # type: ignore[attr-defined]
    )


def _price(price_id: UUID) -> object:
    from skeleton.billing.models import Price

    price = Price.objects.select_related("product").filter(pk=price_id, is_active=True, product__is_public=True).first()
    if price is None:
        raise NotFound()
    return price


class BillingService(ServiceAPI):
    """The organization's billing: standing and quotas for apps, the account for its pages."""

    access_exempt = True

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def access_state(self, *, actor: AbstractBaseUser | None) -> AccessState:
        """How far the organization's access goes, from its billing standing.

        Autorização: qualquer membro (e infraestrutura em ``system_context``).
        Erros: nenhum além do contrato.
        Efeitos: nenhum.
        """
        from skeleton.billing import engine
        from skeleton.common.entitlements import organization_access
        from skeleton.tenancy.context import current_context

        level = organization_access(current_context().organization_id)  # type: ignore[arg-type]
        account = engine.account()
        since = as_utc(account.access_state_since) if account is not None and level.value != "active" else None
        return AccessState(level=level.value, since=since)

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def entitlement(self, *, actor: AbstractBaseUser | None, app_label: str) -> bool:
        """Whether the organization may use ``app_label`` (an app without a price always may).

        Autorização: qualquer membro.
        Erros: nenhum.
        Efeitos: nenhum.
        """
        from skeleton.common.entitlements import entitled_apps
        from skeleton.tenancy.context import current_context

        return entitled_apps(current_context().organization_id, [app_label])[app_label]  # type: ignore[arg-type]

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def storage_allowance(self, *, actor: AbstractBaseUser | None) -> StorageAllowance:
        """The storage quota in force and the bytes in use.

        Autorização: qualquer membro.
        Erros: nenhum.
        Efeitos: nenhum.
        """
        from skeleton.billing import engine
        from skeleton.billing.quota import storage_terms
        from skeleton.files.quota import quota_for, usage_for
        from skeleton.tenancy.context import current_context

        organization_id = current_context().organization_id
        quota, _ = quota_for(organization_id)
        subscription = engine.current_subscription()
        items = engine.live_items(subscription, now=engine.clock()) if subscription is not None else []
        return StorageAllowance(
            quota_bytes=quota,
            used_bytes=int(usage_for(organization_id).bytes_used),
            overage=storage_terms(items)[1],
        )

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def check_quota(self, *, actor: AbstractBaseUser | None, metric: str, increment: int = 1) -> None:
        """Refuse ``increment`` more of ``metric`` above the plan limit.

        Autorização: qualquer membro.
        Erros: ``Conflict("quota_exceeded")`` acima do limite;
        ``InvalidInput`` para métrica não registrada.
        Efeitos: nenhum.
        """
        from skeleton.billing.quota import check_quota
        from skeleton.common.exceptions import QuotaLimitExceeded

        if type(increment) is not int:
            raise InvalidInput({"increment": ("Use um inteiro.",)})
        try:
            check_quota(metric, increment=increment)
        except QuotaLimitExceeded:
            raise Conflict("quota_exceeded") from None
        except ValueError:
            raise InvalidInput({"metric": ("Métrica não registrada.",)}) from None

    @service_method(read_only=True, since="0.6.0")
    def subscription(self, *, actor: AbstractBaseUser) -> SubscriptionSummary | None:
        """The current subscription and its live items.

        Autorização: capacidade ``billing.view``.
        Erros: ``NotFound`` sem a capacidade.
        Efeitos: nenhum.
        """
        from skeleton.billing import engine

        _view(actor)
        subscription = engine.current_subscription()
        return _summary(subscription) if subscription is not None else None

    @service_method(read_only=True, since="0.6.0")
    def list_invoices(
        self, *, actor: AbstractBaseUser, page_size: int = 20, cursor: str | None = None
    ) -> Page[InvoiceSummary]:
        """The organization's invoices, newest first.

        Autorização: capacidade ``billing.view``.
        Erros: ``NotFound`` sem a capacidade.
        Efeitos: nenhum.
        """
        from skeleton.billing.models import Invoice

        _view(actor)
        queryset = Invoice.objects.order_by("-number")
        return paginate(queryset, convert=_invoice, page_size=page_size, cursor=cursor, key=("-number",))

    @service_method(read_only=True, since="0.6.0")
    def offers(self, *, actor: AbstractBaseUser) -> tuple[PriceOffer, ...]:
        """Public active prices the organization can contract (its currency and cycle).

        Autorização: capacidade ``billing.view``.
        Erros: ``NotFound`` sem a capacidade.
        Efeitos: nenhum.
        """
        from skeleton.billing import engine
        from skeleton.billing.models import Price

        _view(actor)
        account = engine.account()
        currency = account.currency if account is not None else engine.default_currency()
        prices = Price.objects.select_related("product").filter(
            is_active=True, product__is_public=True, currency=currency
        )
        subscription = engine.current_subscription()
        if subscription is not None:
            prices = prices.filter(interval=subscription.interval, interval_count=subscription.interval_count)
        return tuple(
            PriceOffer(
                sysid=price.pk,
                product=price.product.name,
                product_code=price.product.code,
                kind=price.product.kind,
                app_label=price.product.app_label,
                currency=price.currency,
                unit_amount=price.unit_amount,
                interval=price.interval,
                interval_count=price.interval_count,
                trial_days=price.trial_days,
                included_storage_bytes=price.included_storage_bytes,
            )
            for price in prices.order_by("product__kind", "unit_amount", "sysid")
        )

    @service_method(read_only=False, since="0.6.0")
    def contract(self, *, actor: AbstractBaseUser, price_id: UUID, terms_version: str = "") -> SubscriptionSummary:
        """Contract ``price_id``: a plan starts or switches the subscription, an app or pack is added.

        Autorização: capacidade ``billing.manage``.
        Erros: ``NotFound`` (preço inexistente ou
        não público); ``InvalidInput`` (moeda ou ciclo diferentes, app já contratada,
        organização em modo restrito).
        Efeitos: fatura proporcional, ativação ao pagar.
        """
        from skeleton.billing import engine
        from skeleton.common.entitlements import AccessLevel, organization_access
        from skeleton.tenancy.context import current_context

        _manage(actor)
        price = _price(price_id)
        if organization_access(current_context().organization_id) in (  # type: ignore[arg-type]
            AccessLevel.RESTRICTED,
            AccessLevel.SUSPENDED,
        ):
            raise Conflict("organization_restricted")
        try:
            if price.product.kind == "base_plan":  # type: ignore[attr-defined]
                engine.change_plan(price)
            else:
                engine.add_item(price)
        except ValidationError as error:
            raise _invalid(error) from None
        if terms_version:
            self._accept_terms(terms_version)
        return _summary(engine.current_subscription())

    def _accept_terms(self, version: str) -> None:
        from skeleton.billing import engine

        account = engine.account(lock=True)
        if account is not None and account.terms_version_accepted != version[:32]:
            account.terms_version_accepted = version[:32]
            account.terms_accepted_at = engine.clock()
            account.save(update_fields={"terms_version_accepted", "terms_accepted_at"})

    @service_method(read_only=False, since="0.6.0")
    def remove_item(self, *, actor: AbstractBaseUser, item_id: UUID) -> SubscriptionSummary | None:
        """End an app or storage pack at the period end (at once while unpaid).

        Autorização: ``billing.manage``.
        Erros: ``NotFound``; ``InvalidInput`` para o plano base.
        Efeitos: o direito termina no fim do período.
        """
        from skeleton.billing import engine
        from skeleton.billing.models import SubscriptionItem

        _manage(actor)
        item = SubscriptionItem.objects.filter(pk=item_id).first()
        if item is None:
            raise NotFound()
        try:
            engine.remove_item(item)
        except ValidationError as error:
            raise _invalid(error) from None
        subscription = engine.current_subscription()
        return _summary(subscription) if subscription is not None else None

    @service_method(read_only=False, since="0.6.0")
    def cancel(self, *, actor: AbstractBaseUser, at_period_end: bool = True) -> SubscriptionSummary | None:
        """Cancel at the period end (at once during a trial); ``resume`` undoes it before the end.

        Autorização: ``billing.manage``.
        Erros: ``InvalidInput`` sem assinatura.
        Efeitos: nenhum dado apagado.
        """
        from skeleton.billing import engine

        _manage(actor)
        try:
            engine.cancel(at_period_end=at_period_end)
        except ValidationError as error:
            raise _invalid(error) from None
        subscription = engine.current_subscription()
        return _summary(subscription) if subscription is not None else None

    @service_method(read_only=False, since="0.6.0")
    def resume(self, *, actor: AbstractBaseUser) -> SubscriptionSummary | None:
        """Undo a cancellation scheduled for the period end.

        Autorização: ``billing.manage``.
        Erros: nenhum.
        Efeitos: a renovação volta a acontecer.
        """
        from skeleton.billing import engine

        _manage(actor)
        subscription = engine.resume()
        return _summary(subscription) if subscription is not None else None

    @service_method(read_only=False, since="0.6.0")
    def redeem_voucher(self, *, actor: AbstractBaseUser, code: str) -> SubscriptionSummary | None:
        """Redeem a voucher code for the subscription.

        Autorização: ``billing.manage``.
        Erros: ``InvalidInput`` (``invalid_voucher``,
        ``already_redeemed``, ``not_trialing``, ``too_many_attempts``, ``no_subscription``).
        Efeitos: desconto nas próximas renovações ou trial estendido.
        """
        from skeleton.billing import engine, vouchers

        _manage(actor)
        try:
            vouchers.redeem(code, user=actor)
        except ValidationError as error:
            raise _invalid(error) from None
        subscription = engine.current_subscription()
        return _summary(subscription) if subscription is not None else None

    @service_method(read_only=False, since="0.6.0")
    def update_account(
        self,
        *,
        actor: AbstractBaseUser,
        legal_name: str,
        billing_email: str,
        tax_id: str,
        country: str,
    ) -> None:
        """Who pays: legal name, billing email, tax id and country.

        Autorização: ``billing.manage``.
        Erros: ``InvalidInput``.
        Efeitos: o provedor recebe os
        dados no próximo checkout.
        """
        from skeleton.billing import engine

        _manage(actor)
        account = engine.ensure_account()
        account = engine.account(lock=True)
        account.legal_name = legal_name.strip()[:200]
        account.billing_email = billing_email.strip()[:254]
        account.tax_id = tax_id.strip()[:32]
        account.country = country.strip().upper()[:2]
        try:
            account.save(update_fields={"legal_name", "billing_email", "tax_id", "country"})
        except ValidationError as error:
            raise _invalid(error) from None

    @service_method(read_only=False, since="0.6.0")
    def checkout(self, *, actor: AbstractBaseUser, invoice_id: UUID, return_url: str) -> CheckoutLink:
        """A hosted payment page for one open invoice of the organization.

        Autorização: ``billing.manage``.
        Erros: ``NotFound`` (fatura inexistente ou de outra
        organização); ``InvalidInput`` (fatura não aberta, sem provedor).
        Efeitos: o provedor
        cria a página; o pagamento chega por webhook.
        """
        from skeleton.billing import payments
        from skeleton.billing.models import Invoice

        _manage(actor)
        if not return_url.startswith(("https://", "http://")):
            raise InvalidInput({"return_url": ("Use uma URL absoluta.",)})
        invoice = Invoice.objects.filter(pk=invoice_id).select_related("account").first()
        if invoice is None:
            raise NotFound()
        try:
            attempt = payments.create_checkout(invoice, return_url=return_url)
        except ValidationError as error:
            raise _invalid(error) from None
        if not attempt.url:
            raise Forbidden()
        return CheckoutLink(url=attempt.url)
