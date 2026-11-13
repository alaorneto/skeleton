"""Platform billing administration (#405): catalog, vouchers, provider accounts and accounts.

Runs on PostgreSQL with the runtime role, with billing on.
"""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from django.urls import reverse

from examples.notes.models import Note
from skeleton.billing import engine
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests import support

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)


@pytest.fixture
def platform(skeleton_db, settings):
    from skeleton.accounts.services import bootstrap_identity

    settings.BILLING_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    scenario = build_service_scenario(Note, slug="billing-platform")
    settings.SKELETON_BILLING_ENABLED = True
    admin, _ = bootstrap_identity(
        email="root@billing-platform.example.test", organization="billing-root", name="Root", password=FIXTURE_PASSWORD
    )
    client = support.login(admin, password=FIXTURE_PASSWORD)  # A fresh login counts as recent authentication.
    return scenario, admin, client


def post(client, name, data, **kwargs):
    return client.post(reverse(name, kwargs=kwargs), {**data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


def test_only_the_platform_reaches_the_billing_pages(platform):
    scenario, admin, client = platform
    assert client.get(reverse("platform:billing")).status_code == 200
    member = support.login(scenario.member, password=FIXTURE_PASSWORD)
    assert member.get(reverse("platform:billing")).status_code == 403
    assert member.get(reverse("platform:organization-billing", kwargs={"sysid": scenario.a.pk})).status_code == 403
    refused = post(member, "platform:billing-product", {"code": "x", "kind": "base_plan", "name": "X"})
    assert refused.status_code == 403


def test_catalog_products_prices_and_vouchers(platform):
    from skeleton.billing.models import Price, Product, Voucher

    _, _, client = platform
    assert (
        post(
            client, "platform:billing-product", {"code": "pro", "kind": "base_plan", "name": "Pro", "is_public": "on"}
        ).status_code
        == 302
    )
    product = Product.objects.get(code="pro")
    created = post(
        client,
        "platform:billing-price",
        {
            "product": product.pk,
            "currency": "BRL",
            "amount": "49.90",
            "interval": "month",
            "included_storage_gb": "1",
            "limits": '{"members": 5}',
        },
    )
    assert created.status_code == 302
    price = Price.objects.get(product=product)
    assert (price.unit_amount, price.included_storage_bytes, price.limits) == (4990, 1024**3, {"members": 5})
    assert (
        post(
            client, "platform:billing-price", {"product": product.pk, "currency": "BRL", "amount": "1.999"}
        ).status_code
        == 400
    )
    flags = post(
        client,
        "platform:billing-price-flags",
        {"is_active": "on", "is_default": "on", "revision": price.revision},
        sysid=price.pk,
    )
    assert flags.status_code == 302 and Price.objects.get(pk=price.pk).is_default
    stale = post(client, "platform:billing-price-flags", {"revision": price.revision}, sysid=price.pk)
    assert stale.status_code == 400  # The revision moved.
    assert (
        post(
            client,
            "platform:billing-voucher",
            {"code": "promo25", "kind": "percent", "value": "25", "duration": "once"},
        ).status_code
        == 302
    )
    assert Voucher.objects.get(code="PROMO25").value == 2500


def test_provider_secrets_are_never_shown_and_an_empty_secret_keeps_them(platform):
    from skeleton.billing.models import ProviderAccount
    from skeleton.testing.billing import use_fake_provider

    _, _, client = platform
    with use_fake_provider() as (_, fake):
        created = post(
            client,
            "platform:billing-provider-create",
            {
                "provider": "fake",
                "label": "Fake 2",
                "mode": "test",
                "api_key": "sk_secret_new",
                "webhook_secret": "whsec_secret_new",
                "enabled": "on",
            },
        )
        assert created.status_code == 302
        account = ProviderAccount.objects.get(label="Fake 2")
        page = client.get(reverse("platform:billing")).content.decode()
        assert "sk_secret_new" not in page and "whsec_secret_new" not in page
        assert f"/billing/webhooks/fake/{account.pk}/" in page
        kept = post(
            client,
            "platform:billing-provider",
            {"label": "Fake 2b", "mode": "test", "enabled": "on", "revision": account.revision},
            sysid=account.pk,
        )
        assert kept.status_code == 302
        account.refresh_from_db()
        assert (account.label, account.secret("api_key")) == ("Fake 2b", "sk_secret_new")


def _subscribed(scenario):
    from skeleton.billing.models import Price, Product

    with system_context(reason="test:platform-catalog"):
        product = Product(code="plan", kind="base_plan", name="Plano")
        product.save()
        price = Price(product=product, currency="BRL", unit_amount=4990)
        price.save()
    with system_context(reason="test:platform-start", organization_id=scenario.a.pk):
        engine.start_subscription(price, now=MAR1)
        invoice = engine.current_subscription().invoices.get()
    return price, invoice


def test_offline_payment_releases_and_actions_need_a_reason(platform):
    from skeleton.billing.models import Invoice, Payment

    scenario, _, client = platform
    _, invoice = _subscribed(scenario)
    page = client.get(reverse("platform:organization-billing", kwargs={"sysid": scenario.a.pk}))
    assert page.status_code == 200 and invoice.number in page.content.decode()
    no_reason = post(
        client,
        "platform:organization-billing-action",
        {"invoice": invoice.pk, "reason": ""},
        sysid=scenario.a.pk,
        action="paid",
    )
    assert no_reason.status_code == 400
    paid = post(
        client,
        "platform:organization-billing-action",
        {"invoice": invoice.pk, "reason": "Transferência 123"},
        sysid=scenario.a.pk,
        action="paid",
    )
    assert paid.status_code == 302
    with system_context(reason="test:platform-read", organization_id=scenario.a.pk):
        assert Invoice.objects.get(pk=invoice.pk).status == "paid"
        payment = Payment.objects.get()
        assert (payment.method, payment.note) == ("offline", "Transferência 123")
        assert engine.current_subscription().status == "active"


def test_void_extend_and_courtesy(platform):
    from skeleton.billing.models import Invoice

    scenario, _, client = platform
    price, invoice = _subscribed(scenario)
    voided = post(
        client,
        "platform:organization-billing-action",
        {"invoice": invoice.pk, "reason": "Cobrança errada"},
        sysid=scenario.a.pk,
        action="void",
    )
    assert voided.status_code == 302
    until = (datetime.now(UTC) + timedelta(days=10)).date().isoformat()
    assert (
        post(
            client,
            "platform:organization-billing-action",
            {"until": until, "reason": "Negociação"},
            sysid=scenario.a.pk,
            action="extend",
        ).status_code
        == 302
    )
    with system_context(reason="test:platform-read-2", organization_id=scenario.a.pk):
        voided_invoice = Invoice.objects.get(pk=invoice.pk)
        assert (voided_invoice.status, voided_invoice.status_note) == ("void", "Cobrança errada")
        assert engine.account().access_override_reason == "Negociação"
    courtesy = post(
        client,
        "platform:organization-billing-action",
        {"price": price.pk, "reason": "Parceiro"},
        sysid=scenario.b.pk,
        action="courtesy",
    )
    assert courtesy.status_code == 302
    with system_context(reason="test:platform-read-b", organization_id=scenario.b.pk):
        subscription = engine.current_subscription()
        assert subscription.status == "active"
        renewal = engine.renew_due(now=subscription.current_period_end)
        assert renewal.total == 0 and renewal.status == "paid"  # The courtesy voucher covers every renewal.


def test_the_billing_platform_role_runs_the_catalog_but_not_accounts(platform):
    from allauth.account.models import EmailAddress

    from skeleton.accounts.models import PlatformRoleAssignment
    from skeleton.accounts.services import create_identity

    scenario, _, _ = platform
    with system_context(reason="test:billing-role"):
        operator = create_identity(email="billing-op@billing-platform.example.test", password=FIXTURE_PASSWORD)
        EmailAddress.objects.create(user=operator, email=operator.email, verified=True, primary=True)
        PlatformRoleAssignment(user=operator, role="billing").save()
    client = support.login(operator, password=FIXTURE_PASSWORD)
    assert client.get(reverse("platform:billing")).status_code == 200
    created = post(client, "platform:billing-product", {"code": "seats", "kind": "storage_pack", "name": "Assentos"})
    assert created.status_code == 302
    assert client.get(reverse("platform:organization-billing", kwargs={"sysid": scenario.a.pk})).status_code == 403
    assert client.get(reverse("platform:users")).status_code == 403
