"""Ledger tables (#542): every table is ``skeleton_ledger_*`` and lives in the organization.

``Book`` is the ``SecuredRecord`` that carries reading and management rights; the other tables
derive their authorization from it and are written only by the services of the app.
Amounts are integers of the minor unit of the book's currency (``skeleton.common.money``).
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext, gettext_lazy

from skeleton.common.models import SecuredRecord


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
