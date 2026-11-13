"""Dunning (#399): the derived access level, notices once per step, cancellation and extensions.

Runs on PostgreSQL with the runtime role, with billing on and an injected clock.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse

from examples.notes.models import Note
from skeleton.billing import dunning, engine
from skeleton.common.entitlements import AccessLevel
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import build_service_scenario
from tests import support

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
APR1 = datetime(2026, 4, 1, tzinfo=UTC)


@pytest.fixture
def billing(skeleton_db, settings, monkeypatch):
    from skeleton.billing.models import Price, Product

    scenario = build_service_scenario(Note, slug="billing-dunning")
    settings.SKELETON_BILLING_ENABLED = True
    clock = {"now": MAR1}
    monkeypatch.setattr(engine, "clock", lambda: clock["now"])
    with system_context(reason="test:dunning-catalog"):
        plan = Product(code="plan", kind="base_plan", name="Plano")
        plan.save()
        price = Price(product=plan, currency="BRL", unit_amount=10000, trial_days=10)
        price.save()
    with system_context(reason="test:dunning-start", organization_id=scenario.a.pk):
        engine.start_subscription(price, now=MAR1)  # Trial until 11 March, first invoice then.
    return scenario, clock


def in_a(scenario):
    return system_context(reason="test:dunning", organization_id=scenario.a.pk)


def invoice_due(scenario):
    with in_a(scenario):
        invoice = engine.renew_due(now=datetime(2026, 3, 11, tzinfo=UTC))
    return invoice


def notices(scenario):
    from skeleton.notifications.models import Notification

    with in_a(scenario):
        return sorted(Notification.objects.filter(type_code__startswith="billing.").values_list("type_code", flat=True))


def test_the_level_follows_the_days_overdue(billing, subtests):
    scenario, clock = billing
    invoice = invoice_due(scenario)
    due = invoice.due_at
    cases = [
        (-1, AccessLevel.ACTIVE),
        (0.5, AccessLevel.PAST_DUE),
        (6.9, AccessLevel.PAST_DUE),
        (7, AccessLevel.RESTRICTED),
        (29.9, AccessLevel.RESTRICTED),
        (30, AccessLevel.SUSPENDED),
        (59, AccessLevel.SUSPENDED),
    ]
    for days, expected in cases:
        with subtests.test(days=days), in_a(scenario):
            billing_account = engine.account()
            assert dunning.derived_level(billing_account, now=due + timedelta(days=days)) is expected


def test_run_stores_the_level_notifies_each_step_once_and_payment_reactivates(billing):
    from skeleton.billing.models import BillingAccount
    from skeleton.events.models import DomainEvent

    scenario, clock = billing
    with in_a(scenario):
        dunning.run(now=datetime(2026, 3, 9, tzinfo=UTC))  # Trial ending in two days.
    invoice = invoice_due(scenario)
    for days in (1, 1, 8, 8, 31, 31):
        clock["now"] = invoice.due_at + timedelta(days=days)
        with in_a(scenario):
            dunning.run(now=clock["now"])
    with in_a(scenario):
        assert BillingAccount.objects.get().access_state == "suspended"
        states = list(
            DomainEvent.objects.filter(event_type="billing.access_state_changed")
            .order_by("occurred_at")
            .values_list("data", flat=True)
        )
        assert [state["access_state"] for state in states] == ["past_due", "restricted", "suspended"]
    assert notices(scenario) == [
        "billing.access_restricted",
        "billing.access_suspended",
        "billing.invoice_issued",
        "billing.invoice_overdue",
        "billing.trial_ending",
    ]
    with in_a(scenario):
        engine.settle_invoice(invoice, now=clock["now"])
        assert BillingAccount.objects.get().access_state == "active"  # At once, no job needed.
    assert "billing.payment_received" in notices(scenario)


def test_restricted_and_suspended_reach_the_surfaces(billing):
    scenario, clock = billing
    invoice = invoice_due(scenario)
    member = support.login(scenario.member, password=scenario.password)
    records = reverse("api:records", kwargs={"organization_id": scenario.a.pk, "table_key": "example_notes.note"})
    clock["now"] = invoice.due_at + timedelta(days=10)
    with in_a(scenario):
        dunning.run(now=clock["now"])
    refused = member.post(
        records,
        {"code": "D-1", "title": "No"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=member.cookies["csrftoken"].value,
    )
    assert (refused.status_code, refused.json()["code"]) == (403, "organization_restricted")
    assert member.get(records).status_code == 200
    clock["now"] = invoice.due_at + timedelta(days=40)
    with in_a(scenario):
        dunning.run(now=clock["now"])
    assert member.get(records).status_code == 403
    from django.test import RequestFactory

    from skeleton.tenancy.middleware import limited_access_response

    request = RequestFactory().get(f"/o/{scenario.a.pk}/billing/")
    request.user = scenario.member
    with scenario.acting(scenario.admin_a):
        assert limited_access_response(request, scenario.a.pk, scenario.admin_a) is None  # The admin settles.
    with scenario.as_member():
        assert limited_access_response(request, scenario.a.pk, scenario.member).status_code == 403


def test_a_pending_payment_holds_the_block(billing):
    from skeleton.billing.models import Payment

    scenario, clock = billing
    invoice = invoice_due(scenario)
    with in_a(scenario):
        Payment(
            organization=scenario.a, invoice=invoice, method="boleto", currency="BRL", amount=10000, provider="fake"
        ).save()
        assert dunning.derived_level(engine.account(), now=invoice.due_at + timedelta(days=10)) is AccessLevel.PAST_DUE
        # The hold lasts the configured days from the payment's creation.
        assert (
            dunning.derived_level(engine.account(), now=datetime.now(UTC) + timedelta(days=4)) is AccessLevel.SUSPENDED
        )


def test_cancellation_keeps_the_organization_suspended_until_paid(billing):
    from skeleton.billing.models import Invoice

    scenario, clock = billing
    note = scenario.create(scenario.a, code="D-2", title="Kept")
    invoice = invoice_due(scenario)
    clock["now"] = invoice.due_at + timedelta(days=61)
    with in_a(scenario):
        assert dunning.run(now=clock["now"]) is AccessLevel.SUSPENDED
        assert engine.current_subscription() is None
        assert Invoice.objects.get(pk=invoice.pk).status == "uncollectible"
        assert Note.objects.filter(pk=note.pk, deleted_at__isnull=True).exists()  # No data is deleted.
        engine.settle_invoice(invoice, now=clock["now"])  # Paid offline later.
        assert dunning.refresh_access(now=clock["now"]) is AccessLevel.ACTIVE


def test_a_platform_extension_keeps_access_until_it_expires(billing):
    scenario, clock = billing
    invoice = invoice_due(scenario)
    late = invoice.due_at + timedelta(days=20)
    with in_a(scenario):
        billing_account = engine.account(lock=True)
        billing_account.access_override_until = late + timedelta(days=2)
        billing_account.access_override_reason = "Negociação com o financeiro"
        billing_account.save(update_fields={"access_override_until", "access_override_reason"})
        assert dunning.run(now=late) is AccessLevel.ACTIVE
        assert dunning.run(now=late + timedelta(days=3)) is AccessLevel.RESTRICTED


def test_auto_charge_retries_follow_the_schedule(billing):
    from skeleton.billing.models import Invoice, Subscription

    scenario, clock = billing
    calls = []
    dunning.register_collector(lambda invoice, now: calls.append((invoice.number, now)))
    try:
        with in_a(scenario):
            Subscription.objects.update(collection="auto_charge")
        invoice = invoice_due(scenario)
        with in_a(scenario):
            dunning.schedule_first_retry(Invoice.objects.get(pk=invoice.pk))
            for days in (0.5, 1, 2, 3, 5, 9):
                dunning.run(now=invoice.due_at + timedelta(days=days))
            assert Invoice.objects.get(pk=invoice.pk).attempt_count == 3
        assert [now - invoice.due_at for _, now in calls] == [timedelta(days=1), timedelta(days=3), timedelta(days=5)]
    finally:
        dunning._collectors.clear()
