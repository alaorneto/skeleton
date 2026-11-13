"""Subscription engine (#396): default plan, trial, renewal, proration and release by payment.

Runs on PostgreSQL with the runtime role, with billing on and an injected clock.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError

from examples.notes.models import Note
from skeleton.billing import engine
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import build_service_scenario

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
APR1 = datetime(2026, 4, 1, tzinfo=UTC)
DAY = 86400


@pytest.fixture
def billing(skeleton_db, settings):
    from skeleton.billing.models import Price, Product

    settings.SKELETON_BILLING_DEFAULT_CURRENCY = "BRL"
    # Organizations A and B exist before billing is on: they start without a subscription.
    scenario = build_service_scenario(Note, slug="billing-engine")
    settings.SKELETON_BILLING_ENABLED = True
    with system_context(reason="test:billing-catalog"):
        basic = Product(code="basic", kind="base_plan", name="Básico")
        pro = Product(code="pro", kind="base_plan", name="Pro")
        notes = Product(code="notes", kind="app", app_label="example_notes", name="Notas")
        for product in (basic, pro, notes):
            product.save()
        prices = {
            "trial": Price(product=basic, currency="BRL", unit_amount=10000, trial_days=14, is_default=True),
            "basic": Price(product=basic, currency="BRL", unit_amount=10000),
            "pro": Price(product=pro, currency="BRL", unit_amount=20000, included_apps=["example_notes"]),
            "notes": Price(product=notes, currency="BRL", unit_amount=3100, included_storage_bytes=1000),
            "yearly": Price(product=notes, currency="BRL", unit_amount=31000, interval="year"),
            "free": Price(product=basic, currency="BRL", unit_amount=0),
        }
        for price in prices.values():
            price.save()
    return scenario, prices


@pytest.fixture
def clock(monkeypatch):
    """The billing clock, moved by the test: rights are judged at it."""
    state = {"now": MAR1}
    monkeypatch.setattr(engine, "clock", lambda: state["now"])
    return state


def codes(error, field="price"):
    return [item.code for item in error.value.error_dict[field]]


def in_org(organization):
    return system_context(reason="test:billing-engine", organization_id=organization.pk)


def notes_available():
    from skeleton.system.applications import application_state

    engine.forget()
    return application_state("example_notes")["available"]


def notes_enabled():
    from skeleton.system.applications import application_state

    engine.forget()
    return application_state("example_notes")["enabled"]


def open_invoices():
    from skeleton.billing.models import Invoice

    return list(Invoice.objects.filter(status="open").order_by("issued_at", "number"))


def test_a_new_organization_starts_in_the_trial_of_the_default_plan(billing):
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.services import create_organization

    with system_context(reason="test:billing-owner"):
        owner = create_identity(email="owner@billing-new.example.test", password="Fixture-only-password-2026!")  # noqa: S106
    before = datetime.now(UTC)
    organization = create_organization(owner=owner, slug="billing-new", name="Nova")
    with in_org(organization):
        subscription = engine.current_subscription()
        assert subscription.status == "trialing" and subscription.account.currency == "BRL"
        assert before + timedelta(days=14) <= subscription.trial_end <= datetime.now(UTC) + timedelta(days=14)
        assert subscription.current_period_end == subscription.trial_end
        assert open_invoices() == []


def test_billing_off_creates_nothing_and_closes_nothing(billing, settings):
    from skeleton.accounts.services import create_identity
    from skeleton.billing.models import BillingAccount
    from skeleton.tenancy.services import create_organization

    settings.SKELETON_BILLING_ENABLED = False
    scenario, _ = billing
    with system_context(reason="test:billing-owner-off"):
        owner = create_identity(email="owner@billing-off.example.test", password="Fixture-only-password-2026!")  # noqa: S106
    organization = create_organization(owner=owner, slug="billing-off", name="Sem cobrança")
    with in_org(organization):
        assert not BillingAccount.objects.exists()
    with in_org(scenario.a):
        assert notes_available()  # A paid app without a subscription stays open while billing is off.


def test_without_a_right_a_paid_app_closes_and_a_free_plan_settles_at_once(billing, clock):
    scenario, prices = billing
    with in_org(scenario.a):
        assert not notes_available()  # Billing on, no subscription: the paid app is closed.
        subscription = engine.start_subscription(prices["free"], now=MAR1)
        [invoice] = subscription.invoices.all()
        assert (invoice.total, invoice.status, invoice.number) == (0, "paid", "2026-000001")
        subscription.refresh_from_db()
        assert subscription.status == "active"


def test_trial_is_granted_once_per_product(billing):
    scenario, prices = billing
    with in_org(scenario.a):
        engine.start_subscription(prices["trial"], now=MAR1)
        engine.cancel(now=MAR1 + timedelta(days=1))  # A trial cancels at once.
        assert engine.current_subscription() is None
        again = engine.start_subscription(prices["trial"], now=MAR1 + timedelta(days=2))
        assert again.status == "incomplete" and again.trial_end is None
        [invoice] = open_invoices()
        assert invoice.total == 10000 and invoice.due_at == MAR1 + timedelta(days=2)


def test_renewal_after_the_trial_invoices_the_period_once(billing):
    scenario, prices = billing
    jan31 = datetime(2026, 1, 31, 9, tzinfo=UTC)
    with in_org(scenario.a):
        subscription = engine.start_subscription(prices["trial"], now=jan31)
        assert engine.renew_due(now=jan31 + timedelta(days=13)) is None  # Not due yet.
        invoice = engine.renew_due(now=subscription.trial_end)
        assert (invoice.period_start, invoice.period_end) == (
            subscription.trial_end,
            datetime(2026, 3, 14, 9, tzinfo=UTC),
        )
        assert invoice.total == 10000 and [line.kind for line in invoice.lines.all()] == ["subscription"]
        assert engine.renew_due(now=subscription.trial_end) is None  # Idempotent for the same period.
        subscription.refresh_from_db()
        assert subscription.status == "active"


def test_the_anchor_day_survives_short_months(billing):
    scenario, prices = billing
    jan31 = datetime(2026, 1, 31, tzinfo=UTC)
    with in_org(scenario.a):
        subscription = engine.start_subscription(prices["basic"], now=jan31)
        assert subscription.current_period_end == datetime(2026, 2, 28, tzinfo=UTC)
        engine.settle_invoice(open_invoices()[0], now=jan31)
        february = engine.renew_due(now=datetime(2026, 2, 28, tzinfo=UTC))
        assert february.period_end == datetime(2026, 3, 31, tzinfo=UTC)


def test_a_contracted_app_is_prorated_and_released_by_the_payment(billing, clock):
    from skeleton.billing.models import SubscriptionItem
    from skeleton.common.entitlements import storage_quota

    scenario, prices = billing
    with scenario.acting(scenario.admin_a):
        from skeleton.testing.service_api import _set_enabled

        _set_enabled("example_notes", scenario.admin_a, enabled=False)
    with in_org(scenario.a):
        engine.start_subscription(prices["basic"], now=MAR1)
        engine.settle_invoice(open_invoices()[0], now=MAR1)
        item = engine.add_item(prices["notes"], now=MAR1 + timedelta(days=15))
        clock["now"] = MAR1 + timedelta(days=15)
        [invoice] = open_invoices()
        # 16 of 31 days of 31.00: 16.00.
        assert [(line.kind, line.amount) for line in invoice.lines.all()] == [("proration", 1600)]
        assert item.status == "pending" and not notes_available()
        engine.settle_invoice(invoice, now=MAR1 + timedelta(days=15))
        assert SubscriptionItem.objects.get(pk=item.pk).status == "active"
        assert notes_available() and notes_enabled()  # The right turned the app on.
        assert storage_quota(scenario.a.pk) == 1000
        with pytest.raises(ValidationError) as twice:
            engine.add_item(prices["notes"], now=MAR1 + timedelta(days=16))
        assert codes(twice) == ["already_subscribed"]
        with pytest.raises(ValidationError) as cycle:
            engine.add_item(prices["yearly"], now=MAR1 + timedelta(days=16))
        assert codes(cycle) == ["price_cycle_mismatch"]


def test_removing_an_app_keeps_the_right_until_the_period_end(billing, clock):
    scenario, prices = billing
    with in_org(scenario.a):
        engine.start_subscription(prices["basic"], now=MAR1)
        engine.settle_invoice(open_invoices()[0], now=MAR1)
        item = engine.add_item(prices["notes"], now=MAR1)
        engine.settle_invoice(open_invoices()[0], now=MAR1)
        engine.remove_item(item, now=MAR1 + timedelta(days=3))
        assert notes_available()
        invoice = engine.renew_due(now=APR1)
        assert [line.description for line in invoice.lines.all()] == ["Básico"]
        clock["now"] = APR1
        assert not notes_available()


def test_removing_an_unpaid_app_voids_its_invoice(billing):
    scenario, prices = billing
    with in_org(scenario.a):
        engine.start_subscription(prices["basic"], now=MAR1)
        engine.settle_invoice(open_invoices()[0], now=MAR1)
        item = engine.add_item(prices["notes"], now=MAR1)
        [invoice] = open_invoices()
        engine.remove_item(item, now=MAR1 + timedelta(hours=1))
        invoice.refresh_from_db()
        assert invoice.status == "void" and open_invoices() == []


def test_upgrade_is_prorated_now_and_downgrade_waits_for_the_period_end(billing, clock):
    scenario, prices = billing
    with in_org(scenario.a):
        engine.start_subscription(prices["basic"], now=MAR1)
        engine.settle_invoice(open_invoices()[0], now=MAR1)
        engine.change_plan(prices["pro"], now=MAR1 + timedelta(days=15))
        clock["now"] = MAR1 + timedelta(days=15)
        [invoice] = open_invoices()
        lines = [(line.kind, line.amount) for line in invoice.lines.order_by("amount")]
        # 16/31 of 200.00 minus the unused 16/31 of 100.00.
        assert lines == [("adjustment", -5161), ("proration", 10323)] and invoice.total == 5162
        assert not notes_available()  # Pro includes the app only once the upgrade is paid.
        engine.settle_invoice(invoice, now=MAR1 + timedelta(days=15))
        assert notes_available()
        subscription = engine.change_plan(prices["basic"], now=MAR1 + timedelta(days=20))
        assert subscription.scheduled_price_id == prices["basic"].pk
        assert notes_available()  # The downgrade waits.
        renewal = engine.renew_due(now=APR1)
        assert [(line.description, line.amount) for line in renewal.lines.all()] == [("Básico", 10000)]
        clock["now"] = APR1
        assert not notes_available()


def test_a_late_first_payment_restarts_the_period(billing):
    scenario, prices = billing
    with in_org(scenario.a):
        subscription = engine.start_subscription(prices["basic"], now=MAR1)
        late = datetime(2026, 5, 10, 8, tzinfo=UTC)
        assert engine.renew_due(now=late) is None
        engine.settle_invoice(open_invoices()[0], now=late)
        subscription.refresh_from_db()
        assert (subscription.status, subscription.current_period_start) == ("active", late)
        assert (subscription.current_period_end, subscription.billing_anchor_day) == (
            datetime(2026, 6, 10, 8, tzinfo=UTC),
            10,
        )


def test_cancel_at_period_end_and_resume(billing):
    scenario, prices = billing
    with in_org(scenario.a):
        engine.start_subscription(prices["basic"], now=MAR1)
        engine.settle_invoice(open_invoices()[0], now=MAR1)
        engine.cancel(now=MAR1 + timedelta(days=5))
        engine.resume()
        assert engine.renew_due(now=APR1) is not None
        engine.cancel(now=APR1 + timedelta(days=1))
        assert engine.renew_due(now=datetime(2026, 5, 1, tzinfo=UTC)) is None
        assert engine.current_subscription() is None


def test_the_cycle_job_closes_every_missed_period(billing):
    from skeleton.billing.jobs import cycle
    from skeleton.billing.models import Invoice

    scenario, prices = billing
    with in_org(scenario.a):
        subscription = engine.start_subscription(prices["basic"], now=datetime(2025, 12, 1, tzinfo=UTC))
        cycle(window="unpaid")
        subscription.refresh_from_db()
        assert subscription.current_period_end == datetime(2026, 1, 1, tzinfo=UTC)  # Never paid: no renewal.
        engine.settle_invoice(open_invoices()[0], now=datetime(2025, 12, 1, tzinfo=UTC))
        cycle(window="now")
        subscription.refresh_from_db()
        assert subscription.current_period_end > datetime.now(UTC)
        numbers = list(Invoice.objects.order_by("number").values_list("number", flat=True))
    assert len(numbers) == len(set(numbers)) >= 3
    with in_org(scenario.b):
        assert not Invoice.objects.exists()  # A/B: the other organization sees nothing.


def test_events_carry_identifiers_and_amounts(billing):
    from skeleton.events.models import DomainEvent

    scenario, prices = billing
    with in_org(scenario.a):
        engine.start_subscription(prices["basic"], now=MAR1)
        engine.settle_invoice(open_invoices()[0], now=MAR1)
        events = list(
            DomainEvent.objects.filter(event_type__startswith="billing.")
            .order_by("occurred_at", "created_at")
            .values_list("event_type", "data")
        )
    codes = [code for code, _ in events]
    assert codes == [
        "billing.subscription_changed",
        "billing.invoice_finalized",
        "billing.subscription_changed",
        "billing.invoice_paid",
    ]
    assert events[1][1] == {"number": "2026-000001", "status": "open", "currency": "BRL", "total": 10000}
