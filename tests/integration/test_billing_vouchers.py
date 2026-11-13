"""Vouchers (#397): kinds and durations over renewals, limits, currency and brute force.

Runs on PostgreSQL with the runtime role, with billing on.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError

from examples.notes.models import Note
from skeleton.billing import engine, vouchers
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import build_service_scenario

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)


def month(n):
    return datetime(2026, 3 + n, 1, tzinfo=UTC) if n < 10 else datetime(2027, n - 9, 1, tzinfo=UTC)


@pytest.fixture
def billing(skeleton_db, settings):
    from skeleton.billing.models import Price, Product

    scenario = build_service_scenario(Note, slug="billing-vouchers")
    settings.SKELETON_BILLING_ENABLED = True
    with system_context(reason="test:voucher-catalog"):
        plan = Product(code="plan", kind="base_plan", name="Plano")
        notes = Product(code="notes", kind="app", app_label="example_notes", name="Notas")
        plan.save()
        notes.save()
        prices = {
            "plan": Price(product=plan, currency="BRL", unit_amount=10000),
            "trial": Price(product=plan, currency="BRL", unit_amount=10000, trial_days=7),
            "notes": Price(product=notes, currency="BRL", unit_amount=2000),
        }
        for price in prices.values():
            price.save()
    return scenario, prices


def voucher(**fields):
    from skeleton.billing.models import Voucher

    with system_context(reason="test:voucher"):
        created = Voucher(**fields)
        created.save()
    return created


def codes(error, field="code"):
    return [item.code for item in error.value.error_dict[field]]


def subscribed(scenario, prices, *, notes=False):
    engine.start_subscription(prices["plan"], now=MAR1)
    engine.settle_invoice(engine.current_subscription().invoices.get(), now=MAR1)
    if notes:
        engine.add_item(prices["notes"], now=MAR1)
        engine.settle_invoice(engine.current_subscription().invoices.get(status="open"), now=MAR1)


def renewal_totals(n):
    totals = []
    for index in range(1, n + 1):
        invoice = engine.renew_due(now=month(index))
        totals.append(invoice.total)
        engine.settle_invoice(invoice, now=month(index))
    return totals


def test_each_kind_and_duration_over_renewals(billing, subtests):
    scenario, prices = billing
    cases = [
        ("pct-once", {"kind": "percent", "value": 2500, "duration": "once"}, [9000, 12000, 12000]),
        (
            "pct-rep",
            {"kind": "percent", "value": 5000, "duration": "repeating", "duration_cycles": 2},
            [6000, 6000, 12000],
        ),
        ("pct-ever", {"kind": "percent", "value": 1000, "duration": "forever"}, [10800, 10800, 10800]),
        ("fixed", {"kind": "fixed", "value": 50000, "currency": "BRL", "duration": "once"}, [0, 12000, 12000]),
        ("free", {"kind": "free_cycles", "value": 0, "duration": "repeating", "duration_cycles": 1}, [0, 12000, 12000]),
        (
            "only-notes",
            {"kind": "percent", "value": 10000, "duration": "forever", "applies_to": ["notes"]},
            [10000] * 3,
        ),
    ]
    for index, (code, fields, expected) in enumerate(cases):
        organization = scenario.a if index % 2 == 0 else scenario.b
        with subtests.test(code=code):
            voucher(code=code.upper(), **fields)
            with system_context(reason="test:voucher-case", organization_id=organization.pk):
                current = engine.current_subscription()
                if current is not None:
                    engine.cancel(now=MAR1, at_period_end=False)
                subscribed(scenario, prices, notes=True)
                vouchers.redeem(f" {code.lower()} ", now=MAR1)
                assert renewal_totals(3) == expected


def test_trial_extension_moves_the_trial_once(billing):
    scenario, prices = billing
    voucher(code="EXTRA", kind="trial_extension", value=10, duration="once")
    with system_context(reason="test:voucher-trial", organization_id=scenario.a.pk):
        subscription = engine.start_subscription(prices["trial"], now=MAR1)
        vouchers.redeem("extra", now=MAR1)
        subscription.refresh_from_db()
        assert subscription.trial_end == MAR1 + timedelta(days=17) == subscription.current_period_end
        assert subscription.items.get().trial_end == subscription.trial_end
        assert engine.renew_due(now=MAR1 + timedelta(days=8)) is None


def test_refusals_are_uniform_and_limits_hold(billing, settings):
    scenario, prices = billing
    voucher(code="USD10", kind="fixed", value=1000, currency="USD", duration="once")
    voucher(code="OLD", kind="percent", value=100, duration="once", valid_until=MAR1 - timedelta(days=1))
    voucher(code="OFF", kind="percent", value=100, duration="once", is_active=False)
    voucher(code="ONE", kind="percent", value=100, duration="once", max_redemptions=1)
    voucher(code="EXT", kind="trial_extension", value=5, duration="once")
    with system_context(reason="test:voucher-a", organization_id=scenario.a.pk):
        with pytest.raises(ValidationError) as none:
            vouchers.redeem("ONE", now=MAR1)
        assert codes(none) == ["no_subscription"]
        subscribed(scenario, prices)
        for code in ("NOPE", "USD10", "OLD", "OFF"):
            with pytest.raises(ValidationError) as refused:
                vouchers.redeem(code, now=MAR1)
            assert codes(refused) == ["invalid_voucher"], code
        with pytest.raises(ValidationError) as trial:
            vouchers.redeem("EXT", now=MAR1)
        assert codes(trial) == ["not_trialing"]
        vouchers.redeem("ONE", now=MAR1)
        with pytest.raises(ValidationError) as twice:
            vouchers.redeem("ONE", now=MAR1)
        assert codes(twice) == ["already_redeemed"]
    with system_context(reason="test:voucher-b", organization_id=scenario.b.pk):
        subscribed(scenario, prices)
        with pytest.raises(ValidationError) as exhausted:
            vouchers.redeem("ONE", now=MAR1)  # The single use went to A.
        assert codes(exhausted) == ["invalid_voucher"]


def test_brute_force_is_rate_limited(billing):
    scenario, prices = billing
    with system_context(reason="test:voucher-brute", organization_id=scenario.a.pk):
        subscribed(scenario, prices)
        seen = set()
        for attempt in range(vouchers.ATTEMPTS_PER_HOUR + 2):
            with pytest.raises(ValidationError) as refused:
                vouchers.redeem(f"GUESS{attempt}", now=MAR1)
            seen.update(codes(refused))
        assert seen == {"invalid_voucher", "too_many_attempts"}


def test_catalog_validation(billing, subtests):
    bad = {
        "percent-over": {"kind": "percent", "value": 10001, "duration": "once"},
        "fixed-no-currency": {"kind": "fixed", "value": 100, "duration": "once"},
        "repeating-no-cycles": {"kind": "percent", "value": 100, "duration": "repeating"},
        "free-forever": {"kind": "free_cycles", "value": 0, "duration": "forever"},
        "bad-code": {"kind": "percent", "value": 100, "duration": "once", "code": "BAD!CODE"},
    }
    for name, fields in bad.items():
        with subtests.test(case=name), pytest.raises(ValidationError):
            voucher(**{"code": name.upper().replace("-", ""), **fields})
    voucher(code="DUP", kind="percent", value=100, duration="once")
    with pytest.raises(ValidationError):
        voucher(code="dup", kind="percent", value=100, duration="once")  # Codes ignore case.
