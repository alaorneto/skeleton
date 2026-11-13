"""Billing catalog and ledger tables: immutability, isolation, audit and numbering (#394).

Runs on PostgreSQL with the runtime role.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction

from skeleton.common.exceptions import ImmutableRecord, UnsafeBulkMutation
from skeleton.tenancy.context import system_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
START = datetime(2026, 3, 1, tzinfo=UTC)
END = datetime(2026, 4, 1, tzinfo=UTC)
PASSWORD = "Fixture-only-password-2026!"  # noqa: S105 - disposable password of a local test fixture


@pytest.fixture
def ledger():
    from skeleton.accounts.services import create_identity
    from skeleton.billing.models import Price, Product
    from skeleton.tenancy.services import create_organization

    with system_context(reason="test:billing-identities"):
        owner_a = create_identity(email="owner-a@billing.example.test", password=PASSWORD)
        owner_b = create_identity(email="owner-b@billing.example.test", password=PASSWORD)
    a = create_organization(owner=owner_a, slug="billing-a", name="Billing A")
    b = create_organization(owner=owner_b, slug="billing-b", name="Billing B")
    with system_context(reason="test:billing-catalog"):
        plan = Product(code="pro", kind=Product.Kind.BASE_PLAN, name="Pro")
        plan.save()
        price = Price(product=plan, currency="BRL", unit_amount=9900, included_storage_bytes=10**9)
        price.save()
    return {"a": a, "b": b, "plan": plan, "price": price}


def _account(organization, **fields):
    from skeleton.billing.models import BillingAccount

    account = BillingAccount(organization=organization, currency="BRL", **fields)
    account.save()
    return account


def _invoice(organization, account, number, **fields):
    from skeleton.billing.models import Invoice

    values = {"currency": "BRL", "subtotal": 9900, "total": 9900, "issued_at": START, "due_at": START + timedelta(7)}
    invoice = Invoice(organization=organization, account=account, number=number, **{**values, **fields})
    invoice.save()
    return invoice


def test_price_terms_never_change_but_the_offer_can_close(ledger):
    from skeleton.billing.models import Price

    price = ledger["price"]
    with system_context(reason="test:billing-price"):
        price.unit_amount = 1
        with pytest.raises(ImmutableRecord):
            price.save()
        price.refresh_from_db()
        price.is_active = False
        price.save()
        assert Price.objects.get(pk=price.pk).is_active is False
        with pytest.raises(UnsafeBulkMutation):
            Price.objects.filter(pk=price.pk).update(unit_amount=1)
        # The database guard refuses it too, whatever the ORM does.
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("UPDATE skeleton.sk_billing_price SET unit_amount = 1 WHERE sysid = %s", [price.pk])


def test_catalog_validation_refuses_bad_terms(ledger, subtests):
    from skeleton.billing.models import Price, Product

    plan = ledger["plan"]
    bad_prices = {
        "currency": {"currency": "XXX"},
        "included_apps": {"included_apps": ["Not A Label"]},
        "repeated_app": {"included_apps": ["notes", "notes"]},
        "limits": {"limits": {"members": -1}},
        "limit_key": {"limits": {"Bad Key": 1}},
        "overage_without_unit": {"storage_overage": "bill"},
    }
    with system_context(reason="test:billing-validation"):
        for name, fields in bad_prices.items():
            with subtests.test(price=name), pytest.raises(ValidationError):
                Price(product=plan, unit_amount=100, **{"currency": "BRL", **fields}).save()
        for name, fields in {
            "app_without_label": {"kind": "app", "app_label": ""},
            "plan_with_label": {"kind": "base_plan", "app_label": "notes"},
            "bad_code": {"kind": "base_plan", "code": "Bad Code"},
            "duplicate_code": {"kind": "base_plan", "code": "pro"},
        }.items():
            with subtests.test(product=name), pytest.raises(ValidationError):
                Product(**{"code": f"p-{name}".replace("_", "-"), "name": "X", **fields}).save()
        app = Product(code="notes-addon", kind="app", app_label="notes", name="Notes")
        app.save()
        Price(product=app, currency="JPY", unit_amount=500, limits={"notes.notes": 100, "members": 5}).save()


def test_ledger_is_isolated_per_organization(ledger):
    from skeleton.billing.models import BillingAccount, Invoice, InvoiceLine, Payment, Subscription

    a, b = ledger["a"], ledger["b"]
    with system_context(reason="test:billing-a", organization_id=a.pk):
        account = _account(a, legal_name="Empresa A Ltda")
        subscription = Subscription(
            organization=a,
            account=account,
            status="active",
            currency="BRL",
            current_period_start=START,
            current_period_end=END,
        )
        subscription.save()
        invoice = _invoice(a, account, "2026-000001", subscription=subscription, period_start=START, period_end=END)
        InvoiceLine(
            organization=a, invoice=invoice, kind="subscription", description="Pro", unit_amount=9900, amount=9900
        ).save()
        Payment(organization=a, invoice=invoice, method="pix", currency="BRL", amount=9900, provider="fake").save()
    with system_context(reason="test:billing-b", organization_id=b.pk):
        for model in (BillingAccount, Subscription, Invoice, InvoiceLine, Payment):
            assert not model.objects.exists(), model
        with pytest.raises(ValidationError):
            # A reference to another organization's account is refused before the database.
            _invoice(b, account, "2026-000002")
    with system_context(reason="test:billing-a-read", organization_id=a.pk):
        assert Invoice.objects.get().lines.count() == 1


def test_billing_account_audit_redacts_personal_data(ledger):
    from skeleton.audit.models import AuditEvent

    a = ledger["a"]
    with system_context(reason="test:billing-audit", organization_id=a.pk):
        account = _account(a, legal_name="Empresa A Ltda", billing_email="fin@a.example.test", tax_id="12345678000199")
        [event] = AuditEvent.objects.filter(record_sysid=account.pk)
    for field in ("legal_name", "billing_email", "tax_id"):
        assert event.changes[field] == {"old": None, "new": "[REDACTED]", "redacted": True}, field
    assert event.changes["currency"]["new"] == "BRL"  # Positive control.


def test_one_account_and_one_live_subscription_per_organization(ledger):
    from skeleton.billing.models import Subscription

    a = ledger["a"]
    with system_context(reason="test:billing-unique", organization_id=a.pk):
        account = _account(a)
        with pytest.raises(ValidationError):
            _account(a)
        values = {"organization": a, "account": account, "currency": "BRL"}
        period = {"current_period_start": START, "current_period_end": END}
        Subscription(status="canceled", canceled_at=START, **values, **period).save()
        Subscription(status="active", **values, **period).save()
        with pytest.raises(ValidationError):
            Subscription(status="trialing", **values, **period).save()
        with pytest.raises(ValidationError):
            Subscription(status="canceled", **values, **period).save()  # Canceled needs canceled_at.


def test_invoice_amounts_are_consistent_and_frozen(ledger):
    a = ledger["a"]
    with system_context(reason="test:billing-invoice", organization_id=a.pk):
        account = _account(a)
        with pytest.raises(ValidationError):
            _invoice(a, account, "2026-000010", total=1)  # total != subtotal - discount + tax.
        with pytest.raises(ValidationError):
            _invoice(a, account, "2026-000011", status="paid")  # Paid needs paid_at and the full amount.
        invoice = _invoice(a, account, "2026-000012", discount_total=900, total=9000)
        invoice.total = 1
        with pytest.raises(ImmutableRecord):
            invoice.save()
        invoice.refresh_from_db()
        invoice.status, invoice.amount_paid, invoice.paid_at = "paid", 9000, START
        invoice.save()  # The collection state moves.
        with pytest.raises(ValidationError):
            _invoice(a, account, "2026-000012")  # Numbers are unique.


def test_invoice_lines_are_never_changed_or_deleted(ledger):
    from skeleton.billing.models import InvoiceLine

    a = ledger["a"]
    with system_context(reason="test:billing-lines", organization_id=a.pk):
        invoice = _invoice(a, _account(a), "2026-000020")
        line = InvoiceLine(
            organization=a, invoice=invoice, kind="subscription", description="Pro", unit_amount=9900, amount=9900
        )
        line.save()
        with pytest.raises(ValidationError):
            InvoiceLine(
                organization=a, invoice=invoice, kind="discount", description="X", unit_amount=1, amount=1
            ).save()
        line.amount = 1
        with pytest.raises(ImmutableRecord):
            line.save()
        line.refresh_from_db()
        with pytest.raises(ImmutableRecord):
            line.delete()
        with pytest.raises(UnsafeBulkMutation):
            InvoiceLine.objects.filter(pk=line.pk).update(deleted_at=START)
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("UPDATE skeleton.sk_billing_invoiceline SET deleted_at = now() WHERE sysid = %s", [line.pk])
        assert InvoiceLine.objects.get(pk=line.pk).deleted_at is None


def test_payment_currency_and_provider_rules(ledger):
    from skeleton.billing.models import Payment

    a = ledger["a"]
    with system_context(reason="test:billing-payments", organization_id=a.pk):
        invoice = _invoice(a, _account(a), "2026-000030")
        values = {"organization": a, "invoice": invoice, "amount": 9900}
        with pytest.raises(ValidationError):
            Payment(method="pix", currency="USD", provider="fake", **values).save()
        with pytest.raises(ValidationError):
            Payment(method="offline", currency="BRL", provider="fake", **values).save()
        with pytest.raises(ValidationError):
            Payment(method="card", currency="BRL", provider="", **values).save()
        Payment(method="card", currency="BRL", provider="fake", provider_reference="ch_1", **values).save()
        with pytest.raises(ValidationError):
            Payment(method="card", currency="BRL", provider="fake", provider_reference="ch_1", **values).save()
        Payment(method="offline", currency="BRL", note="Transferência", **values).save()


def test_invoice_numbers_are_consecutive_and_a_rollback_gives_the_number_back(ledger):
    from skeleton.billing.models import InvoiceCounter
    from skeleton.billing.numbering import next_invoice_number

    with system_context(reason="test:billing-numbers", organization_id=ledger["a"].pk):
        assert next_invoice_number(at=START) == "2026-000001"
        with pytest.raises(RuntimeError), transaction.atomic():
            assert next_invoice_number(at=START) == "2026-000002"
            raise RuntimeError("invoice write failed")
        assert next_invoice_number(at=START) == "2026-000002"
    with system_context(reason="test:billing-numbers-b", organization_id=ledger["b"].pk):
        # Platform wide: another organization continues the same sequence.
        assert next_invoice_number(at=START) == "2026-000003"
        assert next_invoice_number(at=datetime(2027, 1, 1, tzinfo=UTC)) == "2027-000001"
        with pytest.raises(ValidationError):
            next_invoice_number(at=datetime(2026, 1, 1))  # Naive datetime.
    with system_context(reason="test:billing-counters"):
        assert dict(InvoiceCounter.objects.values_list("year", "last_value")) == {2026: 3, 2027: 1}
