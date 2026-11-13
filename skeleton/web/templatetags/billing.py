"""Template filters of the billing pages (#404)."""

from decimal import Decimal

from django import template
from django.utils.formats import number_format

from skeleton.billing.money import exponent, format_amount

register = template.Library()


@register.filter
def money(amount, currency):
    """``4990`` and ``"BRL"`` → ``49,90 BRL`` in the person's locale."""
    try:
        places = exponent(currency)
        text = format_amount(int(amount), currency)
    except (TypeError, ValueError, Exception):  # noqa: BLE001 - a bad value renders as itself
        return amount
    return f"{number_format(Decimal(text), decimal_pos=places, use_l10n=True, force_grouping=True)} {currency}"


@register.filter
def gigabytes(value):
    if value is None:
        return value
    return number_format(Decimal(int(value)) / Decimal(1024**3), decimal_pos=2, use_l10n=True)
