"""Usage of the period on the billing pages and API, and the threshold notices (#525).

Runs on PostgreSQL with the runtime role, with billing on.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.urls import reverse

from examples.notes.models import Note
from skeleton.billing import engine, notices, usage
from skeleton.tenancy.context import system_context
from skeleton.testing.billing import subscribe_with_usage
from skeleton.testing.service_api import build_service_scenario
from tests import support

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
M = "example_notes.messages"
U = "example_notes.uncounted"


@pytest.fixture
def scenario(skeleton_db, settings, monkeypatch):
    monkeypatch.setattr(usage, "_meters", {})
    usage.register_usage_meter(M, label="Mensagens", unit_label="mensagem")
    usage.register_usage_meter(U, label="Sem preço", unit_label="item")
    settings.SKELETON_BILLING_ENABLED = True
    monkeypatch.setattr(engine, "clock", lambda: MAR1 + timedelta(days=4))
    return build_service_scenario(Note, slug="billing-usage-page")


def spend(organization, quantity, key):
    with system_context(reason="test:usage-spend", organization_id=organization.pk):
        return usage.record(M, quantity=quantity, idempotency_key=key)


def threshold_notices(organization):
    from skeleton.notifications.models import Notification

    with system_context(reason="test:usage-notices", organization_id=organization.pk):
        return list(
            Notification.objects.filter(type_code=notices.USAGE_THRESHOLD)
            .order_by("created_at")
            .values("dedupe_key", "payload")
        )


def test_crossed_thresholds():
    assert notices.crossed(0, 79, 100) == ()
    assert notices.crossed(79, 80, 100) == (80,)
    assert notices.crossed(0, 100, 100) == (80, 100)
    assert notices.crossed(80, 99, 100) == ()
    assert notices.crossed(99, 150, 100) == (100,)
    assert notices.crossed(0, 5, None) == () and notices.crossed(0, 5, 0) == ()
    assert notices.crossed(3, 4, 5) == (80,)  # 4 of 5 is 80%.


def test_each_threshold_notifies_once_per_period_without_keys_or_people(scenario):
    subscribe_with_usage(scenario.a, {M: {"included": 10}}, now=MAR1)
    spend(scenario.a, 7, "secret-key-1")
    assert threshold_notices(scenario.a) == []
    spend(scenario.a, 1, "secret-key-2")  # 8 of 10.
    spend(scenario.a, 1, "secret-key-3")  # Still past 80: no second notice.
    spend(scenario.a, 1, "secret-key-4")  # 10 of 10.
    found = threshold_notices(scenario.a)
    period = MAR1.isoformat()
    assert [row["dedupe_key"] for row in found] == [f"usage:{M}:{period}:80", f"usage:{M}:{period}:100"]
    assert set(found[0]["payload"]) == {"meter", "used", "limit", "threshold", "period_start"}
    assert found[1]["payload"]["used"] == 10 and found[1]["payload"]["threshold"] == 100
    assert "secret-key" not in str(found)


def test_the_page_shows_only_contracted_meters_and_keeps_the_capability(scenario):
    page = reverse("billing-overview", kwargs={"organization_id": scenario.a.pk})
    admin = support.login(scenario.admin_a, password=scenario.password)
    assert "Uso neste período" not in admin.get(page).content.decode()  # No subscription: no section.
    subscribe_with_usage(scenario.a, {M: {"included": 10}}, now=MAR1)
    spend(scenario.a, 4, "k1")
    html = admin.get(page).content.decode()
    assert "Uso neste período" in html and "Mensagens" in html and M in html
    assert '<progress max="100" value="40"' in html and U not in html
    assert "<script>" not in html
    member = support.login(scenario.member, password=scenario.password)
    assert member.get(page).status_code == 403
    api = admin.get(reverse("api-billing", kwargs={"organization_id": scenario.a.pk})).json()
    assert [(row["meter"], row["used"], row["limit"]) for row in api["usage"]] == [(M, 4, 10)]


def test_the_platform_sees_the_usage_of_an_organization(scenario):
    from skeleton.accounts.services import bootstrap_identity
    from skeleton.testing.service_api import FIXTURE_PASSWORD

    subscribe_with_usage(scenario.a, {M: {"included": 10}}, now=MAR1)
    spend(scenario.a, 3, "k1")
    root, _ = bootstrap_identity(
        email="root@billing-usage-page.example.test", organization="usage-root", name="Root", password=FIXTURE_PASSWORD
    )
    client = support.login(root, password=FIXTURE_PASSWORD)
    html = client.get(reverse("platform:organization-billing", kwargs={"sysid": scenario.a.pk})).content.decode()
    assert "Uso neste período" in html and M in html and U not in html
