"""Money as integers in the currency's minor unit (#394).

Amounts are never ``float``: a value is an ``int`` of minor units (cents for BRL, yen for
JPY) and the currency says how many decimal places it has. Rounding is half-up, away from
zero for negative amounts (a discount line rounds like the charge it offsets).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

# ISO 4217 minor-unit exponents of the currencies a price may use. A currency outside this
# table is refused: guessing two decimals would misprice JPY or KWD by a factor of 100.
CURRENCY_EXPONENTS = {
    "ARS": 2,
    "AUD": 2,
    "BRL": 2,
    "CAD": 2,
    "CHF": 2,
    "CLP": 0,
    "COP": 2,
    "EUR": 2,
    "GBP": 2,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "MXN": 2,
    "PEN": 2,
    "PYG": 0,
    "USD": 2,
    "UYU": 2,
}
_CODE = re.compile(r"[A-Z]{3}\Z")


def validate_currency(code: str) -> str:
    """Return ``code`` when it is a supported ISO 4217 currency; otherwise ValidationError."""
    if not isinstance(code, str) or not _CODE.match(code) or code not in CURRENCY_EXPONENTS:
        raise ValidationError(gettext("Moeda não suportada: use um código ISO 4217 da tabela de moedas."))
    return code


def exponent(currency: str) -> int:
    return CURRENCY_EXPONENTS[validate_currency(currency)]


def _require_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int of minor units (got {type(value).__name__}).")
    return value


def scale(amount: int, numerator: int, denominator: int) -> int:
    """``amount × numerator / denominator`` rounded half-up (away from zero)."""
    amount = _require_int(amount, "amount")
    numerator = _require_int(numerator, "numerator")
    denominator = _require_int(denominator, "denominator")
    if denominator <= 0:
        raise ValueError("denominator must be positive.")
    if numerator < 0:
        raise ValueError("numerator must not be negative.")
    magnitude = (abs(amount) * numerator * 2 + denominator) // (2 * denominator)
    return -magnitude if amount < 0 else magnitude


def percent(amount: int, basis_points: int) -> int:
    """``basis_points`` hundredths of a percent of ``amount`` (10000 = 100%), half-up."""
    return scale(amount, basis_points, 10000)


def parse(text: str, currency: str) -> int:
    """Minor units of a decimal string such as ``"12.34"``; more decimals than the currency is refused."""
    places = exponent(currency)
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        raise ValidationError(gettext("Valor inválido.")) from None
    if not value.is_finite():
        raise ValidationError(gettext("Valor inválido."))
    minor = value.scaleb(places)
    if minor != minor.to_integral_value():
        raise ValidationError(gettext("O valor tem mais casas decimais que a moeda permite."))
    return int(minor)


def format_amount(amount: int, currency: str) -> str:
    """Plain decimal text of ``amount`` (``1234`` BRL → ``"12.34"``); locale formatting is the template's."""
    amount = _require_int(amount, "amount")
    places = exponent(currency)
    sign = "-" if amount < 0 else ""
    digits = str(abs(amount)).rjust(places + 1, "0")
    if not places:
        return f"{sign}{digits}"
    return f"{sign}{digits[:-places]}.{digits[-places:]}"
