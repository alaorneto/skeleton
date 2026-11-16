"""The billed excess of usage on the renewal invoice (#524).

Runs on PostgreSQL with the runtime role, with billing on.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.db import DatabaseError, connection, transaction

from examples.notes.models import Note
from skeleton.billing import engine, usage
from skeleton.common.exceptions import QuotaLimitExceeded
from skeleton.tenancy.context import system_context
from skeleton.testing.billing import subscribe_with_usage
from skeleton.testing.service_api import build_service_scenario

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
APR1 = datetime(2026, 4, 1, tzinfo=UTC)
M = "example_notes.messages"
S = "example_notes.signatures"
BILL = {"overage": "bill", "unit": 100, "unit_amount": 900}


@pytest.fixture
def scenario(skeleton_db, settings, monkeypatch):
    monkeypatch.setattr(usage, "_meters", {})
    usage.register_usage_meter(M, label="Mensagens", unit_label="mensagem")
    usage.register_usage_meter(S, label="Assinaturas", unit_label="assinatura")
    settings.SKELETON_BILLING_ENABLED = True
    return build_service_scenario(Note, slug="billing-usage-invoice")


def spend(organization, meter, quantity, *, at=MAR1 + timedelta(days=5), key=None):
    with system_context(reason="test:usage-spend", organization_id=organization.pk):
        return usage.record(meter, quantity=quantity, idempotency_key=key or f"{meter}:{quantity}:{at.day}", now=at)


def renew(organization, now=APR1):
    with system_context(reason="test:usage-renew", organization_id=organization.pk):
        return engine.renew_due(now=now)


def overage(invoice):
    with system_context(reason="test:usage-lines", organization_id=invoice.organization_id):
        return list(
            invoice.lines.filter(kind="overage")
            .order_by("metric")
            .values("metric", "quantity", "unit_amount", "amount", "period_start", "period_end", "description")
        )


def test_the_excess_is_billed_once_in_blocks_and_the_line_is_frozen(scenario):
    subscribe_with_usage(scenario.a, {M: {"included": 1000, **BILL}}, now=MAR1)
    spend(scenario.a, M, 1250)
    invoice = renew(scenario.a)
    [line] = overage(invoice)
    assert (line["metric"], line["quantity"], line["unit_amount"], line["amount"]) == (M, 3, 900, 2700)
    assert (line["period_start"], line["period_end"]) == (MAR1, APR1)
    assert "Mensagens" in line["description"]
    assert renew(scenario.a) is None  # Renewing the same period again issues nothing.
    with system_context(reason="test:usage-lines", organization_id=scenario.a.pk):
        assert invoice.lines.filter(kind="overage").count() == 1
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE skeleton.sk_billing_invoiceline SET metric = '' WHERE invoice_id = %s AND kind = 'overage'",
                [invoice.pk],
            )


def test_no_line_within_the_allowance_with_block_or_after_a_trial(scenario):
    subscribe_with_usage(scenario.a, {M: {"included": 1000, **BILL}}, now=MAR1)
    spend(scenario.a, M, 1000)
    assert overage(renew(scenario.a)) == []
    subscribe_with_usage(scenario.b, {M: {"included": 10}}, now=MAR1)
    spend(scenario.b, M, 10)
    assert overage(renew(scenario.b)) == []  # Block: the allowance is the limit, never billed.


def test_a_closing_trial_period_gives_no_line(scenario, settings):
    from skeleton.billing.models import Price, Product

    with system_context(reason="test:usage-trial-catalog"):
        plan = Product(code="trial-plan", kind="base_plan", name="Plano com avaliação")
        plan.save()
        price = Price(product=plan, currency="BRL", unit_amount=1000, trial_days=31, usage={M: {"included": 1, **BILL}})
        price.save()
    with system_context(reason="test:usage-trial", organization_id=scenario.a.pk):
        subscription = engine.start_subscription(price, now=MAR1)
        assert subscription.status == "trialing"
    with pytest.raises(QuotaLimitExceeded):  # A trial blocks at the allowance: no next invoice yet.
        spend(scenario.a, M, 500)
    from skeleton.billing.models import UsageBalance

    with system_context(reason="test:usage-trial", organization_id=scenario.a.pk):
        # Even a counter above the allowance (recorded before a plan change) is not billed for a trial period.
        UsageBalance(organization_id=scenario.a.pk, meter=M, period_start=MAR1, used=500).save()
    invoice = renew(scenario.a)
    assert invoice is not None and overage(invoice) == []


def test_a_pack_contracted_mid_period_raises_the_allowance(scenario):
    from skeleton.billing.models import Price, Product

    subscribe_with_usage(scenario.a, {M: {"included": 1000, **BILL}}, now=MAR1)
    with system_context(reason="test:usage-pack-catalog"):
        pack = Product(code="msg-pack", kind="usage_pack", name="Mensagens extras")
        pack.save()
        price = Price(product=pack, currency="BRL", unit_amount=500, usage={M: {"included": 200}})
        price.save()
    with system_context(reason="test:usage-pack", organization_id=scenario.a.pk):
        engine.add_item(price, now=MAR1 + timedelta(days=10))
        engine.settle_invoice(engine.current_subscription().invoices.get(status="open"), now=MAR1 + timedelta(days=10))
    spend(scenario.a, M, 1250, at=MAR1 + timedelta(days=20))
    [line] = overage(renew(scenario.a))
    assert (line["quantity"], line["amount"]) == (1, 900)  # 1250 - 1200 = 50: one block.


def test_two_meters_give_two_lines_by_code_and_the_voucher_never_passes_the_total(scenario):
    from skeleton.billing import vouchers
    from skeleton.billing.models import Voucher

    subscribe_with_usage(
        scenario.a, {M: {"included": 0, **BILL}, S: {"included": 0, **BILL, "unit": 1, "unit_amount": 50}}, now=MAR1
    )
    spend(scenario.a, S, 3)
    spend(scenario.a, M, 150)
    with system_context(reason="test:usage-voucher"):
        Voucher(code="TUDO", kind="fixed", value=10**9, currency="BRL", duration="forever").save()
    with system_context(reason="test:usage-voucher", organization_id=scenario.a.pk):
        vouchers.redeem("tudo", now=MAR1 + timedelta(days=6))
    invoice = renew(scenario.a)
    assert [(line["metric"], line["amount"]) for line in overage(invoice)] == [(M, 1800), (S, 150)]
    with system_context(reason="test:usage-total", organization_id=scenario.a.pk):
        invoice.refresh_from_db()
        discount = sum(line.amount for line in invoice.lines.filter(kind="discount"))
        charges = sum(line.amount for line in invoice.lines.exclude(kind="discount"))
    assert discount == -charges and invoice.total == 0
