"""Ledger tables (#542): every table is ``skeleton_ledger_*`` and lives in the organization.

``Book`` is the ``SecuredRecord`` that carries reading and management rights; the other tables
derive their authorization from it and are written only by the services of the app.
Amounts are integers of the minor unit of the book's currency (``skeleton.common.money``).
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext, gettext_lazy

from skeleton.common.models import OrganizationRecord, SecuredRecord


class Book(SecuredRecord):
    """A ledger of the organization, in one currency and one basis, both immutable (#542)."""

    class Basis(models.TextChoices):
        CASH = "cash", gettext_lazy("Caixa")
        ACCRUAL = "accrual", gettext_lazy("Competência")

    name = models.CharField(max_length=150)
    currency = models.CharField(max_length=3, default="BRL")
    basis = models.CharField(max_length=16, choices=Basis.choices, default=Basis.CASH)
    is_default = models.BooleanField(default=False)
    locked_until = models.DateField(null=True, blank=True)
    immutable_fields = ("currency", "basis")

    class Meta(SecuredRecord.Meta):
        constraints = SecuredRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization",),
                condition=Q(is_default=True, deleted_at__isnull=True),
                name="skeleton_ledger_one_default_book",
                violation_error_code="unique",
            ),
            models.CheckConstraint(condition=Q(basis__in=("cash", "accrual")), name="skeleton_ledger_book_basis"),
            models.CheckConstraint(condition=Q(currency__regex=r"^[A-Z]{3}$"), name="skeleton_ledger_book_currency"),
        ]
        ordering = ("-is_default", "name", "sysid")

    def clean(self) -> None:
        super().clean()
        from skeleton.common.money import validate_currency

        try:
            validate_currency(self.currency)
        except ValidationError:
            raise ValidationError({"currency": gettext("Moeda não suportada.")}) from None

    def __str__(self) -> str:
        return self.name


class Account(OrganizationRecord):
    """An account of a book's chart (#543); a group account sums its children and takes no line."""

    class Kind(models.TextChoices):
        ASSET = "asset", gettext_lazy("Ativo")
        LIABILITY = "liability", gettext_lazy("Passivo")
        EQUITY = "equity", gettext_lazy("Patrimônio líquido")
        REVENUE = "revenue", gettext_lazy("Receita")
        EXPENSE = "expense", gettext_lazy("Despesa")

    book = models.ForeignKey(Book, on_delete=models.PROTECT, related_name="accounts")
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="children")
    is_group = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    immutable_fields = ("book_id", "code", "kind", "parent_id", "is_group")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("book", "code"),
                condition=Q(deleted_at__isnull=True),
                name="skeleton_ledger_account_live_code",
                violation_error_code="unique",
            ),
            # Target of the composite keys (book, kind, parent) and (book, account) of the migration.
            models.UniqueConstraint(fields=("book", "kind", "sysid"), name="skeleton_ledger_account_book_kind"),
            models.UniqueConstraint(fields=("book", "sysid"), name="skeleton_ledger_account_book_sysid"),
            models.CheckConstraint(
                condition=Q(kind__in=("asset", "liability", "equity", "revenue", "expense")),
                name="skeleton_ledger_account_kind",
            ),
            models.CheckConstraint(
                condition=~Q(parent=models.F("sysid")), name="skeleton_ledger_account_not_own_parent"
            ),
            models.CheckConstraint(condition=~Q(code=""), name="skeleton_ledger_account_code"),
        ]
        ordering = ("code", "sysid")

    def clean(self) -> None:
        super().clean()
        if self.parent_id is None:
            return
        parent = type(self).all_objects.filter(pk=self.parent_id).values("book_id", "kind", "is_group").first()
        if parent is None:
            return
        if parent["book_id"] != self.book_id:
            raise ValidationError({"parent": gettext("A conta-pai é de outro livro.")})
        if parent["kind"] != self.kind:
            raise ValidationError({"parent": gettext("A conta-pai é de outro tipo.")})
        if not parent["is_group"]:
            raise ValidationError({"parent": gettext("A conta-pai precisa ser uma conta-grupo.")})

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class AccountBinding(OrganizationRecord):
    """Which analytic account of the book an account key (registered in code) points to (#543)."""

    book = models.ForeignKey(Book, on_delete=models.PROTECT, related_name="bindings")
    key = models.CharField(max_length=150)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="bindings")
    immutable_fields = ("book_id", "key")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("book", "key"),
                condition=Q(deleted_at__isnull=True),
                name="skeleton_ledger_binding_live_key",
                violation_error_code="unique",
            ),
        ]

    def __str__(self) -> str:
        return self.key


class CostCenter(OrganizationRecord):
    """A cost center of a book (#543)."""

    book = models.ForeignKey(Book, on_delete=models.PROTECT, related_name="cost_centers")
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    immutable_fields = ("book_id", "code")

    class Meta(OrganizationRecord.Meta):
        constraints = OrganizationRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("book", "code"),
                condition=Q(deleted_at__isnull=True),
                name="skeleton_ledger_costcenter_live_code",
                violation_error_code="unique",
            ),
            models.UniqueConstraint(fields=("book", "sysid"), name="skeleton_ledger_costcenter_book_sysid"),
        ]
        ordering = ("code", "sysid")

    def __str__(self) -> str:
        return f"{self.code} {self.name}"
