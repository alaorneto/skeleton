"""Billing surfaces (#404): BillingService, the /o/<org>/billing/ pages and the REST routes.

Runs on PostgreSQL with the runtime role, with billing on and the in-memory FakeProvider.
"""

from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from django.urls import reverse

from examples.notes.models import Note
from skeleton.billing import engine
from skeleton.service_api import errors
from skeleton.service_api.billing import BillingService
from skeleton.tenancy.context import system_context
from skeleton.testing.billing import use_fake_provider
from skeleton.testing.service_api import (
    assert_rejects_other_actor,
    assert_service_contract,
    build_service_scenario,
)
from tests import support

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture
def billing(skeleton_db, settings):
    from skeleton.billing.models import Price, Product

    settings.BILLING_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    scenario = build_service_scenario(Note, slug="billing-surfaces")
    settings.SKELETON_BILLING_ENABLED = True
    with system_context(reason="test:surfaces-catalog"):
        plan = Product(code="plan", kind="base_plan", name="Plano")
        notes = Product(code="notes", kind="app", app_label="example_notes", name="Notas")
        plan.save()
        notes.save()
        prices = {
            "plan": Price(product=plan, currency="BRL", unit_amount=4990),
            "notes": Price(product=notes, currency="BRL", unit_amount=1000),
        }
        for price in prices.values():
            price.save()
    with use_fake_provider() as (provider, account):
        yield scenario, prices, provider


def login(scenario, user):
    return support.login(user, password=scenario.password)


def test_the_service_keeps_the_contract():
    assert_service_contract(BillingService)


def test_reads_for_apps_and_default_deny_for_the_account(billing):
    scenario, prices, _ = billing
    service = BillingService()
    with scenario.acting(scenario.member) as member:
        assert service.access_state(actor=member).level == "active"
        assert service.entitlement(actor=member, app_label="example_notes") is False
        assert service.storage_allowance(actor=member).overage == "block"
        for call in (
            lambda: service.subscription(actor=member),
            lambda: service.list_invoices(actor=member),
            lambda: service.contract(actor=member, price_id=prices["plan"].pk),
        ):
            with pytest.raises(errors.NotFound):
                call()
    assert_rejects_other_actor(
        scenario.caller(lambda actor: service.subscription(actor=scenario.admin_a), user=scenario.member)
    )


def test_the_administrator_contracts_pays_and_the_app_opens(billing):
    scenario, prices, provider = billing
    service = BillingService()
    with scenario.acting(scenario.admin_a) as admin:
        summary = service.contract(actor=admin, price_id=prices["plan"].pk, terms_version="1")
        assert summary.status == "incomplete" and engine.account().terms_version_accepted == "1"
        service.contract(actor=admin, price_id=prices["notes"].pk)
        invoices = service.list_invoices(actor=admin).items
        assert len(invoices) == 2 and all(invoice.status == "open" for invoice in invoices)
        link = service.checkout(actor=admin, invoice_id=invoices[-1].sysid, return_url="https://app.example.test/b")
        assert link.url.startswith("https://fake.example.test/pay/")
        with pytest.raises(errors.InvalidInput):
            service.contract(actor=admin, price_id=prices["notes"].pk)  # Already contracted.
        with pytest.raises(errors.NotFound):
            service.contract(actor=admin, price_id=scenario.a.pk)  # Not a price.
    with scenario.acting(scenario.admin_b, scenario.b) as admin_b:
        assert service.subscription(actor=admin_b) is None  # A/B.
        with pytest.raises(errors.NotFound):
            service.checkout(actor=admin_b, invoice_id=invoices[0].sysid, return_url="https://app.example.test/b")


def test_check_quota_for_apps(billing):
    scenario, prices, _ = billing
    service = BillingService()
    with scenario.acting(scenario.member) as member:
        service.check_quota(actor=member, metric="members")  # No subscription: no limit.
        with pytest.raises(errors.InvalidInput):
            service.check_quota(actor=member, metric="example_notes.unknown")


def test_the_pages_and_routes(billing):
    scenario, prices, provider = billing
    admin = login(scenario, scenario.admin_a)
    page = reverse("billing-overview", kwargs={"organization_id": scenario.a.pk})
    response = admin.get(page)
    assert response.status_code == 200 and "Planos, apps e pacotes" in response.content.decode()
    csrf = admin.cookies["csrftoken"].value
    contract = reverse("billing-action", kwargs={"organization_id": scenario.a.pk, "action": "contract"})
    refused = admin.post(contract, {"price": prices["plan"].pk, "csrfmiddlewaretoken": csrf}, follow=True)
    assert "Aceite os termos" in refused.content.decode()
    admin.post(contract, {"price": prices["plan"].pk, "accept_terms": "on", "csrfmiddlewaretoken": csrf})
    with system_context(reason="test:surfaces-read", organization_id=scenario.a.pk):
        invoice = engine.current_subscription().invoices.get()
    pay = reverse("billing-pay", kwargs={"organization_id": scenario.a.pk, "invoice_id": invoice.pk})
    redirected = admin.post(pay, {"csrfmiddlewaretoken": csrf})
    assert redirected.status_code == 302 and redirected["Location"].startswith("https://fake.example.test/pay/")
    member = login(scenario, scenario.member)
    assert member.get(page).status_code == 403  # No billing capability.
    api = reverse("api-billing", kwargs={"organization_id": scenario.a.pk})
    data = admin.get(api).json()
    assert data["subscription"]["status"] == "incomplete" and data["invoices"][0]["number"] == invoice.number
    assert member.get(api).status_code in (403, 404)
    action = reverse("api-billing-action", kwargs={"organization_id": scenario.a.pk, "action": "voucher"})
    voucher = admin.post(action, {"code": "NOPE"}, content_type="application/json", HTTP_X_CSRFTOKEN=csrf)
    assert voucher.status_code == 400


def test_the_billing_pages_stay_open_while_suspended(billing):
    from skeleton.billing.models import BillingAccount

    scenario, prices, _ = billing
    with system_context(reason="test:surfaces-suspend", organization_id=scenario.a.pk):
        engine.ensure_account()
        BillingAccount.objects.update(access_state="suspended")
    admin = login(scenario, scenario.admin_a)
    assert admin.get(reverse("billing-overview", kwargs={"organization_id": scenario.a.pk})).status_code == 200
    assert admin.get(reverse("web:dashboard", kwargs={"organization_id": scenario.a.pk})).status_code == 403
    member = login(scenario, scenario.member)
    assert member.get(reverse("billing-overview", kwargs={"organization_id": scenario.a.pk})).status_code == 403


def test_the_apps_page_offers_to_contract_an_unpaid_app(billing):
    scenario, prices, _ = billing
    admin = login(scenario, scenario.admin_a)
    page = admin.get(reverse("application-list", kwargs={"organization_id": scenario.a.pk})).content.decode()
    assert "Contratar" in page and reverse("billing-overview", kwargs={"organization_id": scenario.a.pk}) in page
