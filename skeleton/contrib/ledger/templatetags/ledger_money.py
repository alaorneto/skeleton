"""Amounts of the ledger in the page: integers of the minor unit shown in the locale's format (#549)."""

from decimal import Decimal

from django import template
from django.utils.formats import number_format

from skeleton.common.money import exponent, format_amount

register = template.Library()


@register.filter
def money(amount: object, currency: str = "BRL") -> str:
    """``123456`` BRL → ``1.234,56`` in pt-BR; no float on the way."""
    if amount is None or amount == "":
        return "—"
    text = format_amount(int(amount), currency)  # type: ignore[call-overload]
    return number_format(Decimal(text), decimal_pos=exponent(currency), use_l10n=True, force_grouping=True)
