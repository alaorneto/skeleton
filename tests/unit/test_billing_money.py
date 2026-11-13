"""Money in integer minor units, half-up rounding and invoice number format (#394)."""

import pytest
from django.core.exceptions import ValidationError

from skeleton.billing import money
from skeleton.billing.numbering import format_number


def test_scale_rounds_half_up_away_from_zero(subtests):
    cases = [
        # (amount, numerator, denominator, expected)
        (1000, 15, 30, 500),  # Half a month of 10.00.
        (1000, 1, 3, 333),  # 333.33… rounds down.
        (1000, 2, 3, 667),  # 666.66… rounds up.
        (5, 1, 2, 3),  # 2.5 rounds half-up.
        (-5, 1, 2, -3),  # A discount rounds like the charge it offsets.
        (-1000, 1, 3, -333),
        (2999, 31, 31, 2999),  # A whole period is the whole amount.
        (2999, 0, 31, 0),
        (10**15, 29, 31, 935483870967742),  # No float precision loss.
    ]
    for amount, numerator, denominator, expected in cases:
        with subtests.test(amount=amount, numerator=numerator, denominator=denominator):
            assert money.scale(amount, numerator, denominator) == expected


def test_percent_uses_basis_points(subtests):
    for amount, points, expected in [(1999, 1000, 200), (1999, 10000, 1999), (1999, 0, 0), (1, 5000, 1)]:
        with subtests.test(amount=amount, points=points):
            assert money.percent(amount, points) == expected


def test_non_integer_amounts_and_bad_ratios_are_refused():
    with pytest.raises(TypeError):
        money.scale(10.5, 1, 2)
    with pytest.raises(TypeError):
        money.scale(True, 1, 2)
    with pytest.raises(ValueError):
        money.scale(10, 1, 0)
    with pytest.raises(ValueError):
        money.scale(10, -1, 2)


def test_currency_exponents_parse_and_format(subtests):
    cases = [
        ("BRL", "12.34", 1234, "12.34"),
        ("BRL", "0.05", 5, "0.05"),
        ("JPY", "1500", 1500, "1500"),
        ("KWD", "1.250", 1250, "1.250"),
        ("USD", "-3.10", -310, "-3.10"),
    ]
    for currency, text, minor, formatted in cases:
        with subtests.test(currency=currency, text=text):
            assert money.parse(text, currency) == minor
            assert money.format_amount(minor, currency) == formatted


def test_unsupported_currency_and_extra_decimals_are_refused(subtests):
    for currency in ("brl", "XXX", "BR", "", None):
        with subtests.test(currency=currency), pytest.raises(ValidationError):
            money.validate_currency(currency)
    for text, currency in [("12.345", "BRL"), ("1.5", "JPY"), ("abc", "BRL"), ("NaN", "BRL"), ("Infinity", "USD")]:
        with subtests.test(text=text, currency=currency), pytest.raises(ValidationError):
            money.parse(text, currency)


def test_invoice_number_format(settings):
    settings.SKELETON_BILLING_INVOICE_PREFIX = ""
    assert format_number(2026, 7) == "2026-000007"
    settings.SKELETON_BILLING_INVOICE_PREFIX = "ACME-"
    assert format_number(2026, 1234567) == "ACME-2026-1234567"


def test_billing_check_requires_the_keyring_only_when_enabled(settings):
    from skeleton.billing.checks import billing_checks

    settings.SKELETON_BILLING_ENABLED = False
    settings.BILLING_ENCRYPTION_KEYS = []
    assert billing_checks() == []
    settings.SKELETON_BILLING_ENABLED = True
    [error] = billing_checks()
    assert error.id == "skeleton.E037" and "BILLING_ENCRYPTION_KEYS" in error.msg
    settings.BILLING_ENCRYPTION_KEYS = ["ZmFrZS1rZXktZm9yLWEtdGVzdC1vbmx5LTMyLWJ5dGU="]
    assert billing_checks() == []
