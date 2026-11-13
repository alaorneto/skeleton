"""Quotas (#398): storage allowance with block or billed overage, daily snapshots and plan limits.

Runs on PostgreSQL with the runtime role, with billing on.
"""

from datetime import UTC, date, datetime

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from examples.notes.models import Note
from skeleton.billing import engine
from skeleton.common.exceptions import QuotaLimitExceeded
from skeleton.system.registry import AttachmentPolicy
from skeleton.tenancy.context import system_context
from skeleton.testing.registries import table_spec_override
from skeleton.testing.service_api import build_service_scenario
from tests import support

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
APR1 = datetime(2026, 4, 1, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 92  # 100 bytes


@pytest.fixture
def billing(skeleton_db, settings, monkeypatch):
    from skeleton.billing.models import Price, Product

    key = "example_notes.note"
    with table_spec_override(key, attachments=AttachmentPolicy(max_bytes=1000, types=("image/png",))):
        settings.SKELETON_FILES_ORG_QUOTA_BYTES = 10_000
        scenario = build_service_scenario(Note, slug="billing-quota")
        settings.SKELETON_BILLING_ENABLED = True
        monkeypatch.setattr(engine, "clock", lambda: MAR1)
        with system_context(reason="test:quota-catalog"):
            plan = Product(code="plan", kind="base_plan", name="Plano")
            seats = Product(code="seats", kind="storage_pack", name="Assentos")
            plan.save()
            seats.save()
            prices = {
                "block": Price(product=plan, currency="BRL", unit_amount=1000, included_storage_bytes=150),
                "bill": Price(
                    product=plan,
                    currency="BRL",
                    unit_amount=1000,
                    included_storage_bytes=100,
                    storage_overage="bill",
                    overage_unit_bytes=50,
                    overage_unit_amount=700,
                    overage_hard_cap_bytes=300,
                ),
                "members": Price(product=plan, currency="BRL", unit_amount=1000, limits={"members": 4}),
                "seats": Price(product=seats, currency="BRL", unit_amount=100, limits={"members": 1}),
            }
            for price in prices.values():
                price.save()
        yield scenario, prices


def subscribe(organization, price):
    with system_context(reason="test:quota-subscribe", organization_id=organization.pk):
        engine.start_subscription(price, now=MAR1)
        engine.settle_invoice(engine.current_subscription().invoices.get(), now=MAR1)


def upload(scenario, organization, note):
    client = support.login(scenario.member, password=scenario.password)
    target = reverse(
        "api:attachments",
        kwargs={"organization_id": organization.pk, "table_key": "example_notes.note", "sysid": note.pk},
    )
    return client.post(
        target, {"file": SimpleUploadedFile("f.png", PNG)}, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value
    )


def test_block_mode_refuses_above_the_allowance_and_the_platform_override_wins(billing):
    from skeleton.files.models import StorageQuota

    scenario, prices = billing
    subscribe(scenario.a, prices["block"])
    note = scenario.create(scenario.a, code="Q-1", title="Q")
    assert upload(scenario, scenario.a, note).status_code == 201
    refused = upload(scenario, scenario.a, note)
    assert (refused.status_code, refused.json()["code"]) == (409, "quota_exceeded")
    with system_context(reason="test:quota-override"):
        StorageQuota(quota_organization=scenario.a, quota_bytes=None).save()
    assert upload(scenario, scenario.a, note).status_code == 201


def test_bill_mode_accepts_up_to_the_cap_and_invoices_the_peak(billing):
    from skeleton.billing.models import UsageSnapshot
    from skeleton.billing.quota import snapshot_usage

    scenario, prices = billing
    subscribe(scenario.a, prices["bill"])
    note = scenario.create(scenario.a, code="Q-2", title="Q")
    assert [upload(scenario, scenario.a, note).status_code for _ in range(4)] == [201, 201, 201, 409]
    with system_context(reason="test:quota-snapshot", organization_id=scenario.a.pk):
        snapshot_usage(now=datetime(2026, 3, 10, tzinfo=UTC))
        UsageSnapshot(organization=scenario.a, day=date(2026, 3, 20), bytes_used=290).save()  # The peak.
        UsageSnapshot(organization=scenario.a, day=date(2026, 4, 1), bytes_used=999).save()  # Next period.
        snapshot = UsageSnapshot.objects.get(day=date(2026, 3, 10))
        assert (snapshot.bytes_used, snapshot.by_app) == (300, {"example_notes": 300})
        invoice = engine.renew_due(now=APR1)
        overage = invoice.lines.get(kind="overage")
        # Peak 290, allowance 100: 190 bytes in blocks of 50 → 4 blocks of 7.00.
        assert (overage.quantity, overage.amount) == (4, 2800)
        assert invoice.total == 1000 + 2800


def test_snapshots_are_per_organization_and_expire(billing, settings):
    from skeleton.billing.models import UsageSnapshot
    from skeleton.billing.quota import purge_snapshots, snapshot_usage

    scenario, prices = billing
    note = scenario.create(scenario.a, code="Q-3", title="Q")
    assert upload(scenario, scenario.a, note).status_code == 201
    with system_context(reason="test:quota-b", organization_id=scenario.b.pk):
        assert snapshot_usage(now=MAR1).bytes_used == 0  # A's attachment is not B's usage.
    settings.SKELETON_BILLING_USAGE_RETENTION_DAYS = 31
    with system_context(reason="test:quota-a", organization_id=scenario.a.pk):
        UsageSnapshot(organization=scenario.a, day=date(2026, 1, 1), bytes_used=1).save()
        snapshot_usage(now=MAR1)
        assert purge_snapshots(now=MAR1) == 1
        assert list(UsageSnapshot.objects.values_list("day", flat=True)) == [date(2026, 3, 1)]


def test_member_limit_counts_the_plan_and_the_packs(billing):
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership, update_membership
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.models import Membership

    scenario, prices = billing
    subscribe(scenario.a, prices["members"])  # A has 3 members: admin, member, denied.
    with system_context(reason="test:quota-people"):
        extra = [
            create_identity(email=f"extra{n}@billing-quota.example.test", password=scenario.password) for n in range(3)
        ]
    with system_context(reason="test:quota-members", organization_id=scenario.a.pk):
        role = Role.objects.get(code="member")
        provision_membership(organization=scenario.a, user=extra[0], role=role)
        with pytest.raises(QuotaLimitExceeded) as refused:
            provision_membership(organization=scenario.a, user=extra[1], role=role)
        assert refused.value.limit == 4
        engine.add_item(prices["seats"], now=MAR1)
        engine.settle_invoice(engine.current_subscription().invoices.get(status="open"), now=MAR1)
        provision_membership(organization=scenario.a, user=extra[1], role=role)  # One more seat.
        membership = Membership.objects.get(user=scenario.denied)
    with scenario.acting(scenario.admin_a) as admin:
        membership = update_membership(membership, user=admin, data={"is_active": False})
    with system_context(reason="test:quota-members-2", organization_id=scenario.a.pk):
        provision_membership(organization=scenario.a, user=extra[2], role=role)  # The freed seat.
    with scenario.acting(scenario.admin_a) as admin:
        with pytest.raises(QuotaLimitExceeded):
            update_membership(Membership.objects.get(pk=membership.pk), user=admin, data={"is_active": True})
    with system_context(reason="test:quota-members-b", organization_id=scenario.b.pk):
        provision_membership(organization=scenario.b, user=extra[0], role=Role.objects.get(code="member"))


def test_an_app_metric_is_checked_by_the_billing_quota(billing):
    from skeleton.billing import quota
    from skeleton.billing.models import Price, Product
    from skeleton.service_api.internal_errors import classify

    scenario, _ = billing
    counter = lambda organization_id: Note.objects.count()  # noqa: E731
    quota.register_quota_metric("example_notes.notes", counter)
    try:
        with system_context(reason="test:quota-app-catalog"):
            plan = Product(code="small", kind="base_plan", name="Pequeno")
            plan.save()
            price = Price(product=plan, currency="BRL", unit_amount=1, limits={"example_notes.notes": 1})
            price.save()
        subscribe(scenario.a, price)
        with system_context(reason="test:quota-app", organization_id=scenario.a.pk):
            quota.check_quota("example_notes.notes")
            Note(organization=scenario.a, owner=scenario.member, code="N-1", title="One").save()
            with pytest.raises(QuotaLimitExceeded) as refused:
                quota.check_quota("example_notes.notes")
            assert classify(refused.value).code == "quota_exceeded"
            with pytest.raises(ValueError):
                quota.check_quota("example_notes.unknown")
    finally:
        quota._metrics.pop("example_notes.notes", None)
