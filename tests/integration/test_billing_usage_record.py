"""Recording and voiding usage (#523): idempotency, limits, periods, isolation and the service contract.

Runs on PostgreSQL with the runtime role, with billing on.
"""

from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection

from examples.notes.models import Note
from skeleton.billing import engine, usage
from skeleton.common.exceptions import MissingTenantContext
from skeleton.service_api import Conflict, Forbidden, InvalidInput, NotFound
from skeleton.service_api.billing import BillingService
from skeleton.tenancy.context import system_context
from skeleton.testing.billing import subscribe_with_usage
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

pytestmark = pytest.mark.needs_example_app
MAR1 = datetime(2026, 3, 1, tzinfo=UTC)
APR1 = datetime(2026, 4, 1, tzinfo=UTC)
M = "example_notes.messages"


@pytest.fixture
def metered(skeleton_db, settings, monkeypatch):
    monkeypatch.setattr(usage, "_meters", {})
    usage.register_usage_meter(M, label="Mensagens", unit_label="mensagem")
    scenario = build_service_scenario(Note, slug="billing-usage")
    settings.SKELETON_BILLING_ENABLED = True
    monkeypatch.setattr(engine, "clock", lambda: MAR1 + timedelta(days=3))
    subscribe_with_usage(scenario.a, {M: {"included": 3}}, now=MAR1)
    return scenario


def record(scenario, key, quantity=1, organization=None, **kwargs):
    return scenario.caller(
        lambda actor: BillingService().record_usage(
            actor=actor, meter=M, idempotency_key=key, quantity=quantity, **kwargs
        ),
        organization=organization,
    )()


def events(organization):
    from skeleton.billing.models import UsageEvent

    with system_context(reason="test:usage-read", organization_id=organization.pk):
        return list(
            UsageEvent.objects.order_by("created_at").values_list("idempotency_key", "voided_at", "period_start")
        )


def test_the_contract_of_the_new_methods(metered):
    assert_service_contract(BillingService)
    assert_rejects_other_actor(
        metered.caller(lambda actor: BillingService().record_usage(actor=metered.admin_a, meter=M, idempotency_key="x"))
    )
    assert_unavailable_when_disabled(
        metered.caller(lambda actor: BillingService().record_usage(actor=actor, meter=M, idempotency_key="y")),
        disabled=metered.application_disabled,
    )


def test_recording_is_idempotent_and_blocks_at_the_allowance(metered):
    first = record(metered, "msg:1")
    assert (first.metered, first.created, first.used, first.included, first.limit, first.overage) == (
        True,
        True,
        1,
        3,
        3,
        "block",
    )
    again = record(metered, "msg:1")
    assert (again.created, again.used) == (False, 1)
    assert record(metered, "msg:2", quantity=2).used == 3
    with pytest.raises(Conflict) as error:
        record(metered, "msg:3")
    assert error.value.code == "quota_exceeded"
    assert [key for key, _, _ in events(metered.a)] == ["msg:1", "msg:2"]
    with pytest.raises(Conflict):
        metered.caller(lambda actor: BillingService().check_usage(actor=actor, meter=M))()
    [status] = metered.caller(lambda actor: BillingService().usage(actor=actor))()
    assert (status.meter, status.label, status.used, status.limit, status.period_start, status.period_end) == (
        M,
        "Mensagens",
        3,
        3,
        MAR1,
        APR1,
    )
    for bad in ({"quantity": 0}, {"quantity": 1_000_001}, {"key": "tem espaço"}, {"key": "x" * 201}):
        with pytest.raises(InvalidInput):
            record(metered, bad.get("key", "ok"), quantity=bad.get("quantity", 1))
    with pytest.raises(InvalidInput):
        metered.caller(
            lambda actor: BillingService().record_usage(actor=actor, meter="example_notes.x", idempotency_key="z")
        )()


def test_billed_overage_stops_at_the_hard_cap_and_runs_free_without_one(metered):
    terms = {M: {"included": 2, "overage": "bill", "unit": 1, "unit_amount": 10, "hard_cap": 4}}
    subscribe_with_usage(metered.b, terms, now=MAR1)
    for number in range(4):
        assert record(metered, f"b:{number}", organization=metered.b).used == number + 1
    with pytest.raises(Conflict):
        record(metered, "b:4", organization=metered.b)
    assert record(metered, "a:only", organization=metered.a).used == 1  # A's counter is its own.


def test_voiding_gives_the_unit_back_once_and_a_voided_key_is_not_reused(metered):
    record(metered, "msg:1")
    record(metered, "msg:2")
    voided = metered.caller(lambda actor: BillingService().void_usage(actor=actor, meter=M, idempotency_key="msg:1"))()
    assert voided.used == 1
    twice = metered.caller(lambda actor: BillingService().void_usage(actor=actor, meter=M, idempotency_key="msg:1"))()
    assert twice.used == 1
    with pytest.raises(Conflict) as error:
        record(metered, "msg:1")
    assert error.value.code == "usage_voided"
    with pytest.raises(NotFound):
        metered.caller(lambda actor: BillingService().void_usage(actor=actor, meter=M, idempotency_key="nunca"))()


def test_a_consumption_after_the_end_goes_to_the_next_period_and_a_closed_one_cannot_be_voided(metered, monkeypatch):
    from skeleton.billing.models import Subscription

    record(metered, "march")
    late = record(metered, "late", occurred_at=APR1 + timedelta(hours=2))  # Before the renewal ran.
    assert late.used == 1  # A counter of its own, for the next period.
    assert sorted(start for _, _, start in events(metered.a)) == [MAR1, APR1]
    with system_context(reason="test:usage-renewal", organization_id=metered.a.pk):
        subscription = Subscription.objects.get()
        subscription.current_period_start, subscription.current_period_end = APR1, datetime(2026, 5, 1, tzinfo=UTC)
        subscription.save(update_fields={"current_period_start", "current_period_end"})
    monkeypatch.setattr(engine, "clock", lambda: APR1 + timedelta(days=1))
    with pytest.raises(Conflict) as error:
        metered.caller(lambda actor: BillingService().void_usage(actor=actor, meter=M, idempotency_key="march"))()
    assert error.value.code == "usage_period_closed"


def test_nothing_is_recorded_without_billing_or_a_subscription(metered, settings):
    receipt = record(metered, "none", organization=metered.b)  # B has no subscription.
    assert (receipt.metered, receipt.created, receipt.limit) == (False, False, None)
    settings.SKELETON_BILLING_ENABLED = False
    assert record(metered, "off").metered is False
    assert events(metered.a) == [] and events(metered.b) == []


def test_organizations_never_see_or_count_each_other(metered):
    from skeleton.billing.models import UsageBalance, UsageEvent

    record(metered, "a:1")
    with system_context(reason="test:usage-b", organization_id=metered.b.pk):
        assert not UsageEvent.objects.exists() and not UsageBalance.objects.exists()
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM skeleton.sk_billing_usageevent")
            assert cursor.fetchone() == (0,)
            cursor.execute("SELECT count(*) FROM skeleton.sk_billing_usagebalance")
            assert cursor.fetchone() == (0,)
    with pytest.raises(MissingTenantContext), system_context(reason="test:usage-no-tenant"):
        usage.record(M, idempotency_key="nowhere")


def test_the_system_context_may_record_and_a_member_may_not_pass_no_actor(metered):
    with system_context(reason="test:usage-system", organization_id=metered.a.pk):
        assert BillingService().record_usage(actor=None, meter=M, idempotency_key="job:1").used == 1
    with acting_as(metered.member, metered.a), pytest.raises(Forbidden):
        BillingService().record_usage(actor=None, meter=M, idempotency_key="job:2")


def test_retention_deletes_only_old_periods(metered, settings):
    from skeleton.billing.models import UsageBalance, UsageEvent

    record(metered, "current")
    with system_context(reason="test:usage-old", organization_id=metered.a.pk):
        old = datetime(2024, 1, 1, tzinfo=UTC)
        previous = datetime(2026, 2, 1, tzinfo=UTC)
        for key, start in (("old", old), ("previous", previous)):
            UsageEvent(
                organization_id=metered.a.pk,
                meter=M,
                quantity=1,
                occurred_at=start,
                period_start=start,
                idempotency_key=key,
                source_app="example_notes",
            ).save()
            UsageBalance(organization_id=metered.a.pk, meter=M, period_start=start, used=1).save()
        settings.SKELETON_BILLING_USAGE_RETENTION_DAYS = 1  # Even a tiny retention keeps the previous period.
        assert usage.purge_usage() == 2
        assert set(UsageEvent.objects.values_list("idempotency_key", flat=True)) == {"current", "previous"}
        assert UsageBalance.objects.count() == 2
