"""Catalog, billing accounts, subscriptions and the invoice ledger (#394).

The catalog (products and prices) is global and written by the platform. Everything an
organization owns (its billing account, subscription, invoices and payments) is an
``OrganizationRecord`` written only by the billing services. Amounts are integers of the
currency's minor unit (``skeleton.billing.money``).
"""

import re

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy

from skeleton.common.exceptions import ImmutableRecord
from skeleton.common.models import OrganizationRecord, SystemRecord

from .money import validate_currency

_CODE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_APP_LABEL = re.compile(r"[a-z][a-z0-9_]*\Z")
# A plan limit names a core metric ("members") or an app metric ("<app_label>.<name>").
_LIMIT_KEY = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?\Z")
CURRENCY = Q(currency__regex=r"^[A-Z]{3}$")


class Interval(models.TextChoices):
    MONTH = "month", gettext_lazy("Mês")
    YEAR = "year", gettext_lazy("Ano")


class Product(SystemRecord):
    """Something the platform sells: a base plan, an app or a storage pack."""

    class Kind(models.TextChoices):
        BASE_PLAN = "base_plan", gettext_lazy("Plano base")
        APP = "app", gettext_lazy("App")
        STORAGE_PACK = "storage_pack", gettext_lazy("Pacote de armazenamento")

    code = models.CharField(max_length=64)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    # The app this product sells (kind=app only): its subscription item grants the app.
    app_label = models.CharField(max_length=100, blank=True, default="")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    is_public = models.BooleanField(default=True)
    immutable_fields = ("code", "kind", "app_label")

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("code",),
                condition=Q(deleted_at__isnull=True),
                name="sk_billing_product_code_live",
                violation_error_code="unique",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=("base_plan", "app", "storage_pack")), name="sk_billing_product_kind"
            ),
            models.CheckConstraint(
                condition=(Q(kind="app") & ~Q(app_label="")) | (~Q(kind="app") & Q(app_label="")),
                name="sk_billing_product_app_label",
            ),
        ]

    def clean(self):
        super().clean()
        if not _CODE.match(self.code or ""):
            raise ValidationError({"code": gettext("Use letras minúsculas, dígitos, _ ou -, começando por letra.")})
        if self.kind == self.Kind.APP and not _APP_LABEL.match(self.app_label or ""):
            raise ValidationError({"app_label": gettext("Informe o label da app vendida.")})

    def __str__(self):
        return self.code


class Price(SystemRecord):
    """One price of a product. Its terms never change: a new price replaces it (grandfathering)."""

    class Overage(models.TextChoices):
        BLOCK = "block", gettext_lazy("Bloquear acima da franquia")
        BILL = "bill", gettext_lazy("Cobrar o excedente")

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="prices")
    currency = models.CharField(max_length=3)
    unit_amount = models.PositiveBigIntegerField()
    interval = models.CharField(max_length=8, choices=Interval.choices, default=Interval.MONTH)
    interval_count = models.PositiveSmallIntegerField(default=1)
    trial_days = models.PositiveSmallIntegerField(default=0)
    included_storage_bytes = models.PositiveBigIntegerField(default=0)
    # Base plans only: apps entitled without their own item.
    included_apps = models.JSONField(default=list, blank=True)
    # {"members": 10, "<app_label>.<metric>": N}: absent metric means unlimited.
    limits = models.JSONField(default=dict, blank=True)
    storage_overage = models.CharField(max_length=8, choices=Overage.choices, default=Overage.BLOCK)
    overage_unit_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    overage_unit_amount = models.PositiveBigIntegerField(null=True, blank=True)
    overage_hard_cap_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    # The only term the platform changes: an inactive price is not offered to new subscriptions.
    is_active = models.BooleanField(default=True)
    immutable_fields = (
        "product_id",
        "currency",
        "unit_amount",
        "interval",
        "interval_count",
        "trial_days",
        "included_storage_bytes",
        "included_apps",
        "limits",
        "storage_overage",
        "overage_unit_bytes",
        "overage_unit_amount",
        "overage_hard_cap_bytes",
    )

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.CheckConstraint(condition=CURRENCY, name="sk_billing_price_currency"),
            models.CheckConstraint(condition=Q(interval__in=("month", "year")), name="sk_billing_price_interval"),
            models.CheckConstraint(condition=Q(interval_count__gte=1), name="sk_billing_price_interval_count"),
            models.CheckConstraint(
                condition=Q(storage_overage="block")
                | Q(storage_overage="bill", overage_unit_bytes__gte=1, overage_unit_amount__gte=1),
                name="sk_billing_price_overage",
            ),
        ]

    def clean(self):
        super().clean()
        try:
            validate_currency(self.currency)
        except ValidationError as exc:
            raise ValidationError({"currency": exc.messages}) from None
        apps = self.included_apps
        if not isinstance(apps, list) or not all(isinstance(app, str) and _APP_LABEL.match(app) for app in apps):
            raise ValidationError({"included_apps": gettext("Use uma lista de labels de apps.")})
        if len(set(apps)) != len(apps):
            raise ValidationError({"included_apps": gettext("App repetida.")})
        limits = self.limits
        if not isinstance(limits, dict) or not all(
            isinstance(key, str) and _LIMIT_KEY.match(key) and type(value) is int and value >= 0
            for key, value in limits.items()
        ):
            raise ValidationError({"limits": gettext("Use métricas com limites inteiros não negativos.")})
        if self.storage_overage == self.Overage.BILL and not (self.overage_unit_bytes and self.overage_unit_amount):
            raise ValidationError({"storage_overage": gettext("Informe o bloco e o valor do excedente.")})

    def __str__(self):
        return f"{self.product_id} {self.unit_amount} {self.currency}/{self.interval}"


class InvoiceCounter(SystemRecord):
    """Last invoice number of a year, platform wide: invoices are issued by the platform."""

    year = models.PositiveSmallIntegerField()
    last_value = models.PositiveBigIntegerField(default=0)
    immutable_fields = ("year",)

    class Meta(SystemRecord.Meta):
        constraints = SystemRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("year",),
                condition=Q(deleted_at__isnull=True),
                name="sk_billing_invoicecounter_year_live",
                violation_error_code="unique",
            ),
        ]

    def __str__(self):
        return f"{self.year}: {self.last_value}"


class BillingAccount(OrganizationRecord):
    """Who pays for an organization, and how far its access goes (one per organization)."""

    class AccessState(models.TextChoices):
        ACTIVE = "active", gettext_lazy("Em dia")
        PAST_DUE = "past_due", gettext_lazy("Em atraso")
        RESTRICTED = "restricted", gettext_lazy("Somente leitura")
        SUSPENDED = "suspended", gettext_lazy("Suspensa")

    legal_name = models.CharField(max_length=200, blank=True, default="")
    billing_email = models.EmailField(blank=True, default="")
    tax_id = models.CharField(max_length=32, blank=True, default="")
    country = models.CharField(max_length=2, blank=True, default="")
    currency = models.CharField(max_length=3)
    access_state = models.CharField(max_length=16, choices=AccessState.choices, default=AccessState.ACTIVE)
    access_state_since = models.DateTimeField(default=timezone.now)
    terms_version_accepted = models.CharField(max_length=32, blank=True, default="")
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization",),
                condition=Q(deleted_at__isnull=True),
                name="sk_billing_account_organization_live",
                violation_error_code="unique",
            ),
            models.CheckConstraint(condition=CURRENCY, name="sk_billing_account_currency"),
            models.CheckConstraint(
                condition=Q(access_state__in=("active", "past_due", "restricted", "suspended")),
                name="sk_billing_account_access_state",
            ),
        ]

    def clean(self):
        super().clean()
        try:
            validate_currency(self.currency)
        except ValidationError as exc:
            raise ValidationError({"currency": exc.messages}) from None

    def __str__(self):
        return f"{self.organization_id} {self.access_state}"


class Subscription(OrganizationRecord):
    """The organization's recurring contract; every item shares its currency and cycle."""

    class Status(models.TextChoices):
        TRIALING = "trialing", gettext_lazy("Em avaliação")
        ACTIVE = "active", gettext_lazy("Ativa")
        PAST_DUE = "past_due", gettext_lazy("Em atraso")
        CANCELED = "canceled", gettext_lazy("Cancelada")
        INCOMPLETE = "incomplete", gettext_lazy("Aguardando pagamento")

    class Collection(models.TextChoices):
        AUTO_CHARGE = "auto_charge", gettext_lazy("Cobrança automática")
        SEND_INVOICE = "send_invoice", gettext_lazy("Fatura para pagamento")

    account = models.ForeignKey(BillingAccount, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=16, choices=Status.choices)
    currency = models.CharField(max_length=3)
    interval = models.CharField(max_length=8, choices=Interval.choices, default=Interval.MONTH)
    interval_count = models.PositiveSmallIntegerField(default=1)
    current_period_start = models.DateTimeField()
    current_period_end = models.DateTimeField()
    trial_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)
    collection = models.CharField(max_length=16, choices=Collection.choices, default=Collection.SEND_INVOICE)
    immutable_fields = ("account_id", "currency", "interval", "interval_count")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            # One live subscription per organization; a canceled one stays as history.
            models.UniqueConstraint(
                fields=("organization",),
                condition=Q(deleted_at__isnull=True) & ~Q(status="canceled"),
                name="sk_billing_subscription_current",
                violation_error_code="unique",
            ),
            models.CheckConstraint(condition=CURRENCY, name="sk_billing_subscription_currency"),
            models.CheckConstraint(
                condition=Q(status__in=("trialing", "active", "past_due", "canceled", "incomplete")),
                name="sk_billing_subscription_status",
            ),
            models.CheckConstraint(
                condition=Q(current_period_end__gt=F("current_period_start")), name="sk_billing_subscription_period"
            ),
            models.CheckConstraint(
                condition=Q(status="canceled", canceled_at__isnull=False) | (~Q(status="canceled")),
                name="sk_billing_subscription_canceled_at",
            ),
            models.CheckConstraint(
                condition=Q(collection__in=("auto_charge", "send_invoice")), name="sk_billing_subscription_collection"
            ),
        ]

    def __str__(self):
        return f"{self.organization_id} {self.status}"


class SubscriptionItem(OrganizationRecord):
    """One priced line of a subscription: the base plan, an app or a storage pack.

    The rows are the history too: an ended item keeps its dates, which is how a second
    trial of the same product in the same organization is refused.
    """

    class Status(models.TextChoices):
        PENDING = "pending", gettext_lazy("Aguardando pagamento")
        ACTIVE = "active", gettext_lazy("Ativo")
        ENDED = "ended", gettext_lazy("Encerrado")

    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name="items")
    price = models.ForeignKey(Price, on_delete=models.PROTECT, related_name="subscription_items")
    # Copy of the product's app label, so the entitlement check reads one table.
    app_label = models.CharField(max_length=100, blank=True, default="")
    quantity = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    trial_end = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("subscription_id", "price_id", "app_label")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(quantity__gte=1), name="sk_billing_item_quantity"),
            models.CheckConstraint(
                condition=Q(status__in=("pending", "active", "ended")), name="sk_billing_item_status"
            ),
            models.CheckConstraint(
                condition=Q(ends_at__isnull=True) | Q(ends_at__gte=F("starts_at")), name="sk_billing_item_dates"
            ),
        ]
        indexes = [models.Index(fields=("organization", "app_label", "status"), name="sk_billing_item_app")]

    def __str__(self):
        return f"{self.subscription_id} {self.price_id} x{self.quantity}"


class Invoice(OrganizationRecord):
    """An amount the organization owes; its numbers never change once issued.

    Only the collection state moves (status, amount paid, dates), always by the billing
    services with the record's revision.
    """

    class Status(models.TextChoices):
        OPEN = "open", gettext_lazy("Em aberto")
        PAID = "paid", gettext_lazy("Paga")
        VOID = "void", gettext_lazy("Anulada")
        UNCOLLECTIBLE = "uncollectible", gettext_lazy("Incobrável")

    account = models.ForeignKey(BillingAccount, on_delete=models.PROTECT, related_name="invoices")
    subscription = models.ForeignKey(
        Subscription, on_delete=models.PROTECT, null=True, blank=True, related_name="invoices"
    )
    number = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    currency = models.CharField(max_length=3)
    subtotal = models.PositiveBigIntegerField()
    discount_total = models.PositiveBigIntegerField(default=0)
    tax_total = models.PositiveBigIntegerField(default=0)
    total = models.PositiveBigIntegerField()
    amount_paid = models.PositiveBigIntegerField(default=0)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    issued_at = models.DateTimeField()
    due_at = models.DateTimeField()
    paid_at = models.DateTimeField(null=True, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = (
        "account_id",
        "subscription_id",
        "number",
        "currency",
        "subtotal",
        "discount_total",
        "tax_total",
        "total",
        "period_start",
        "period_end",
        "issued_at",
    )

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(condition=CURRENCY, name="sk_billing_invoice_currency"),
            models.CheckConstraint(
                condition=Q(status__in=("open", "paid", "void", "uncollectible")), name="sk_billing_invoice_status"
            ),
            models.CheckConstraint(
                condition=Q(total=F("subtotal") - F("discount_total") + F("tax_total")),
                name="sk_billing_invoice_total",
            ),
            models.CheckConstraint(condition=Q(amount_paid__lte=F("total")), name="sk_billing_invoice_paid_amount"),
            models.CheckConstraint(
                condition=~Q(status="paid") | Q(paid_at__isnull=False, amount_paid=F("total")),
                name="sk_billing_invoice_paid",
            ),
            models.CheckConstraint(
                condition=~Q(status="void") | Q(voided_at__isnull=False), name="sk_billing_invoice_voided"
            ),
            models.CheckConstraint(
                condition=Q(period_start__isnull=True, period_end__isnull=True) | Q(period_end__gt=F("period_start")),
                name="sk_billing_invoice_period",
            ),
        ]
        indexes = [models.Index(fields=("organization", "status", "due_at"), name="sk_billing_invoice_due")]

    def __str__(self):
        return self.number


class InvoiceLine(OrganizationRecord):
    """One line of an invoice. Written with the invoice and never changed or deleted.

    The tombstone columns are frozen too, so not even a soft delete can hide a line.
    """

    class Kind(models.TextChoices):
        SUBSCRIPTION = "subscription", gettext_lazy("Assinatura")
        PRORATION = "proration", gettext_lazy("Proporcional")
        OVERAGE = "overage", gettext_lazy("Excedente")
        DISCOUNT = "discount", gettext_lazy("Desconto")
        ADJUSTMENT = "adjustment", gettext_lazy("Ajuste")

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="lines")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    description = models.CharField(max_length=300)
    price = models.ForeignKey(Price, on_delete=models.PROTECT, null=True, blank=True, related_name="invoice_lines")
    app_label = models.CharField(max_length=100, blank=True, default="")
    quantity = models.PositiveIntegerField(default=1)
    # Signed: a discount line is negative.
    unit_amount = models.BigIntegerField()
    amount = models.BigIntegerField()
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    immutable_fields = (
        "invoice_id",
        "kind",
        "description",
        "price_id",
        "app_label",
        "quantity",
        "unit_amount",
        "amount",
        "period_start",
        "period_end",
        "deleted_at",
        "deleted_by",
    )

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(
                condition=Q(kind__in=("subscription", "proration", "overage", "discount", "adjustment")),
                name="sk_billing_line_kind",
            ),
            models.CheckConstraint(condition=Q(quantity__gte=1), name="sk_billing_line_quantity"),
            models.CheckConstraint(
                condition=~Q(kind="discount") | Q(amount__lte=0), name="sk_billing_line_discount_sign"
            ),
        ]

    def delete(self, using=None, keep_parents=False):
        raise ImmutableRecord("Invoice lines are never deleted.")

    def __str__(self):
        return f"{self.invoice_id} {self.kind} {self.amount}"


class Payment(OrganizationRecord):
    """One attempt to pay an invoice, by a provider or registered offline by the platform."""

    class Method(models.TextChoices):
        CARD = "card", gettext_lazy("Cartão")
        PIX = "pix", gettext_lazy("Pix")
        BOLETO = "boleto", gettext_lazy("Boleto")
        OFFLINE = "offline", gettext_lazy("Pagamento registrado manualmente")

    class Status(models.TextChoices):
        PENDING = "pending", gettext_lazy("Pendente")
        SUCCEEDED = "succeeded", gettext_lazy("Confirmado")
        FAILED = "failed", gettext_lazy("Recusado")
        REFUNDED = "refunded", gettext_lazy("Estornado")
        CANCELED = "canceled", gettext_lazy("Cancelado")

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=8, choices=Method.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    currency = models.CharField(max_length=3)
    amount = models.PositiveBigIntegerField()
    # Empty for an offline payment; otherwise the provider's code and its id of the charge.
    provider = models.CharField(max_length=32, blank=True, default="")
    provider_reference = models.CharField(max_length=200, blank=True, default="")
    failure_code = models.CharField(max_length=64, blank=True, default="")
    note = models.CharField(max_length=500, blank=True, default="")
    succeeded_at = models.DateTimeField(null=True, blank=True)
    immutable_fields = ("invoice_id", "method", "currency", "amount", "provider")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.CheckConstraint(condition=CURRENCY, name="sk_billing_payment_currency"),
            models.CheckConstraint(
                condition=Q(method__in=("card", "pix", "boleto", "offline")), name="sk_billing_payment_method"
            ),
            models.CheckConstraint(
                condition=Q(status__in=("pending", "succeeded", "failed", "refunded", "canceled")),
                name="sk_billing_payment_status",
            ),
            models.CheckConstraint(condition=Q(amount__gte=1), name="sk_billing_payment_amount"),
            models.CheckConstraint(
                condition=Q(method="offline", provider="") | (~Q(method="offline") & ~Q(provider="")),
                name="sk_billing_payment_provider",
            ),
            models.UniqueConstraint(
                fields=("provider", "provider_reference"),
                condition=~Q(provider_reference=""),
                name="sk_billing_payment_reference",
                violation_error_code="unique",
            ),
        ]

    def clean(self):
        super().clean()
        if self.invoice_id and self._state.adding:
            invoice = Invoice.all_objects.filter(pk=self.invoice_id).values("currency").first()
            if invoice and invoice["currency"] != self.currency:
                raise ValidationError({"currency": gettext("A moeda do pagamento difere da fatura.")})

    def __str__(self):
        return f"{self.invoice_id} {self.method} {self.status}"
