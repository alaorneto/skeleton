"""Usage terms of a price (#522): stored, frozen by the guard, and typed in the platform form.

Runs on PostgreSQL with the runtime role.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.urls import reverse

from skeleton.billing import usage
from skeleton.tenancy.context import system_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
M = "acme_reminders.messages"
TERMS = {M: {"included": 1000, "overage": "bill", "unit": 100, "unit_amount": 900, "hard_cap": 20000}}


@pytest.fixture
def meter(monkeypatch):
    monkeypatch.setattr(usage, "_meters", {})
    usage.register_usage_meter(M, label="Mensagens", unit_label="mensagem")


def test_usage_terms_are_stored_validated_and_never_change(meter):
    from skeleton.billing.models import Price, Product
    from skeleton.common.exceptions import ImmutableRecord

    with system_context(reason="test:billing-usage-terms"):
        plan = Product(code="pro", kind=Product.Kind.BASE_PLAN, name="Pro")
        plan.save()
        pack = Product(code="mensagens", kind=Product.Kind.USAGE_PACK, name="Mensagens extras")
        pack.save()
        price = Price(product=plan, currency="BRL", unit_amount=9900, usage=TERMS)
        price.save()
        assert Price.objects.get(pk=price.pk).usage == TERMS
        with pytest.raises(ValidationError) as error:
            Price(product=pack, currency="BRL", unit_amount=1000, usage=TERMS).save()
        assert "usage" in error.value.message_dict
        Price(product=pack, currency="BRL", unit_amount=1000, usage={M: {"included": 5000}}).save()
        price.usage = {M: {"included": 1}}
        with pytest.raises(ImmutableRecord):
            price.save()
        # The database guard refuses it too, whatever the ORM does.
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE skeleton.sk_billing_price SET usage = '{}'::jsonb WHERE sysid = %s",
                [price.pk],
            )
        price.refresh_from_db()
        price.is_active = False  # The offer still closes.
        price.save()


@pytest.mark.needs_example_app
def test_the_platform_price_form_keeps_what_was_typed_and_lists_the_meters(meter, settings):
    from cryptography.fernet import Fernet

    from skeleton.accounts.services import bootstrap_identity
    from skeleton.billing.models import Price, Product
    from skeleton.testing.service_api import FIXTURE_PASSWORD
    from tests import support

    settings.BILLING_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    settings.SKELETON_BILLING_ENABLED = True
    admin, _ = bootstrap_identity(
        email="root@billing-usage.example.test", organization="billing-usage", name="Root", password=FIXTURE_PASSWORD
    )
    client = support.login(admin, password=FIXTURE_PASSWORD)
    with system_context(reason="test:billing-usage-form"):
        plan = Product(code="pro", kind=Product.Kind.BASE_PLAN, name="Pro")
        plan.save()
    page = client.get(reverse("platform:billing")).content.decode()
    assert M in page and "Pacote de uso" in page

    def post(terms):
        return client.post(
            reverse("platform:billing-price"),
            {
                "product": plan.pk,
                "currency": "BRL",
                "amount": "99.00",
                "trial_days": "7",
                "usage": terms,
                "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            },
        )

    refused = post('{"acme_reminders.unknown": {"included": 1}}')
    html = refused.content.decode()
    assert refused.status_code == 400
    assert "Medidor não registrado" in html and M in html
    assert "acme_reminders.unknown" in html and 'value="99.00"' in html and 'value="7"' in html
    assert post('{"acme_reminders.messages": {"included": 1000, "overage": "bill"}}').status_code == 400
    assert post("not json").status_code == 400
    import json

    assert post(json.dumps(TERMS)).status_code == 302
    with system_context(reason="test:billing-usage-form"):
        assert Price.objects.get(product=plan).usage == TERMS
