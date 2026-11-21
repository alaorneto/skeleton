"""An event consumer numbers a record and opens a ledger title, idempotently (#577).

The example notes app plays the consuming app: a note stands for an order, and the consumer of
``example_notes.order_completed`` opens one charge per order: a note numbered by the app's own
gapless sequence and a receivable title whose source is the order. The consumer runs as the event
delivery job does, in ``system_context`` of the organization, without an actor. The service calls
go through ``as_app``, a frame of the example app's package, which is how the core recognises the
calling app (``require_owning_app``). Runtime role on PostgreSQL throughout.
"""

import datetime as dt

import pytest
from ledger_consumer import NOTE

from skeleton.contrib.ledger.service_api import LedgerService
from skeleton.contrib.ledger.testing import enable_ledger, prepare_book
from skeleton.service_api import ContextRequired, Forbidden, NotFound
from skeleton.service_api.events import EventService
from skeleton.service_api.sequences import SequenceService
from skeleton.tenancy.context import system_context
from skeleton.testing.events import capture_events
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import acting_as, build_service_scenario

pytestmark = pytest.mark.needs_example_app

APP_MODULE = "examples.notes.charges"
CHARGE = "example_notes.charge"
COMPLETED = "example_notes.order_completed"
CONSUMER = "example_notes.open_charge"
REVENUE = "example_notes.service_revenue"


def as_module(module, function):
    """Run ``function`` from a frame whose module is ``module`` (how an app's code calls the service)."""
    namespace = {"__name__": module, "function": function}
    exec("def call(*args, **kwargs):\n    return function(*args, **kwargs)\n", namespace)  # noqa: S102
    return namespace["call"]


def as_app(function):
    return as_module(APP_MODULE, function)


def open_charge(envelope):
    """The consumer of the example app: one numbered charge and one receivable per completed order."""
    from examples.notes.models import Note

    ledger = LedgerService()
    if as_app(ledger.titles_for_source)(actor=None, table_key=envelope.table_key, record_id=envelope.record_sysid):
        return  # A repeated delivery: the order already has its charge.
    number = as_app(SequenceService().next_number)(actor=None, key=CHARGE, at=envelope.occurred_at)
    Note(
        organization_id=envelope.organization_id,
        owner_id=envelope.actor_id,
        code=number.formatted,
        title=f"Cobrança do pedido {envelope.data['code']}",
    ).save()
    as_app(ledger.create_title)(
        actor=None,
        direction="receivable",
        amount=12000,
        due_date=envelope.occurred_at.date() + dt.timedelta(days=30),
        description=f"Cobrança {number.formatted}",
        category_key=REVENUE,
        idempotency_key=f"charge:{envelope.event_id}",
        source_table=envelope.table_key,
        source_id=envelope.record_sysid,
    )


@pytest.fixture
def shop(skeleton_db, monkeypatch):
    """Organizations A/B with the example app and the ledger; the charge sequence and the consumer declared."""
    import ledger_consumer  # noqa: F401 - registers the consumer keys of the example app

    from examples.notes.models import Note
    from skeleton.events import registry as events
    from skeleton.events.registry import EventType, register_consumer, register_event_type
    from skeleton.sequences import registry as sequences
    from skeleton.sequences.registry import SequenceSpec, register_sequence

    monkeypatch.setattr(sequences, "_sequences", dict(sequences._sequences))
    monkeypatch.setattr(events, "_types", dict(events._types))
    monkeypatch.setattr(events, "_consumers", dict(events._consumers))
    register_sequence(SequenceSpec(CHARGE, period="year", template="CB-{year}-{number:04d}", owner_app="example_notes"))
    register_event_type(EventType(COMPLETED, table_key=NOTE, payload_fields=("code",), owner_app="example_notes"))
    register_consumer(COMPLETED, CONSUMER, open_charge)
    scenario = build_service_scenario(Note, slug="event-charges")
    for organization in (scenario.a, scenario.b):
        enable_ledger(organization)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        prepare_book(admin, bindings={REVENUE: "4.1"})
    return scenario


def complete(scenario, code):
    """The member completes an order: the app publishes the event in the business transaction."""
    order = scenario.create(scenario.a, code=code, title=f"Pedido {code}")
    with capture_events() as published, acting_as(scenario.member, scenario.a) as actor:
        as_app(EventService().publish)(actor=actor, event_type=COMPLETED, record_id=order.pk, data=(("code", code),))
    assert [event.event_type for event in published] == [COMPLETED]
    return order, published[0]


def charges(scenario, organization=None):
    from examples.notes.models import Note

    with system_context(reason="test:read-charges", organization_id=(organization or scenario.a).pk):
        return sorted(Note.objects.filter(code__startswith="CB-").values_list("code", "title"))


def test_the_consumer_numbers_the_charge_and_opens_the_title_once(shop):
    from skeleton.events.consumers import envelope

    first, event = complete(shop, "PD-1")
    outcomes = run_pending_jobs()
    assert outcomes and all(outcome.state == "succeeded" for outcome in outcomes), outcomes
    year = event.occurred_at.year
    assert charges(shop) == [(f"CB-{year}-0001", "Cobrança do pedido PD-1")]
    ledger = LedgerService()
    with system_context(reason="test:read-title", organization_id=shop.a.pk):
        (title,) = ledger.titles_for_source(actor=None, table_key=NOTE, record_id=first.pk)
    assert (title.description, title.amount, title.source_id) == (f"Cobrança CB-{year}-0001", 12000, first.pk)

    # A repeated delivery (at least once) changes nothing and draws no number.
    with system_context(reason="job:events.consume", organization_id=shop.a.pk):
        open_charge(envelope(event))
    assert charges(shop) == [(f"CB-{year}-0001", "Cobrança do pedido PD-1")]
    with system_context(reason="test:read-title", organization_id=shop.a.pk):
        assert len(ledger.titles_for_source(actor=None, table_key=NOTE, record_id=first.pk)) == 1

    # The next order gets the next number: no gap, and organization B sees none of it.
    second, _ = complete(shop, "PD-2")
    assert all(outcome.state == "succeeded" for outcome in run_pending_jobs())
    assert [code for code, _ in charges(shop)] == [f"CB-{year}-0001", f"CB-{year}-0002"]
    assert charges(shop, shop.b) == []
    with system_context(reason="test:read-other-tenant", organization_id=shop.b.pk):
        with pytest.raises(NotFound):
            ledger.title(actor=None, title_id=title.sysid)
        with pytest.raises(NotFound):
            ledger.titles_for_source(actor=None, table_key=NOTE, record_id=first.pk)


def test_the_consumer_reads_the_revision_that_cancel_title_needs(shop):
    complete(shop, "PD-1")
    run_pending_jobs()
    ledger = LedgerService()
    with system_context(reason="job:events.consume", organization_id=shop.a.pk):
        (found,) = ledger.titles_for_source(actor=None, table_key=NOTE, record_id=_order(shop, "PD-1"))
        current = ledger.title(actor=None, title_id=found.sysid)
        assert ledger.settlements(actor=None, title_id=found.sysid) == ()
        cancelled = ledger.cancel_title(actor=None, title_id=current.sysid, expected_revision=current.revision)
    assert cancelled.status == "cancelled" and cancelled.revision == current.revision + 1


def _order(scenario, code):
    from examples.notes.models import Note

    with system_context(reason="test:read-order", organization_id=scenario.a.pk):
        return Note.objects.get(code=code).pk


def test_numbering_in_system_context_stays_with_the_owning_app(shop, subtests):
    service = SequenceService()
    with subtests.test("another app's code"), system_context(reason="test:other-app", organization_id=shop.a.pk):
        with pytest.raises(Forbidden):
            as_module("skeleton.contrib.ledger.consumers", service.next_number)(actor=None, key=CHARGE)
    with subtests.test("code of no app"), system_context(reason="test:no-app", organization_id=shop.a.pk):
        with pytest.raises(Forbidden):
            service.next_number(actor=None, key=CHARGE)
    with subtests.test("no organization"), system_context(reason="test:global"):
        with pytest.raises(ContextRequired):
            as_app(service.next_number)(actor=None, key=CHARGE)
    with subtests.test("no actor outside system"), acting_as(shop.member, shop.a):
        with pytest.raises(Forbidden):
            as_app(service.next_number)(actor=None, key=CHARGE)
    with system_context(reason="test:first-number", organization_id=shop.a.pk):
        assert as_app(service.next_number)(actor=None, key=CHARGE).number == 1  # The refusals drew nothing.


def test_reads_without_an_actor_need_the_system_context(shop):
    complete(shop, "PD-1")
    run_pending_jobs()
    order = _order(shop, "PD-1")
    ledger = LedgerService()
    with acting_as(shop.member, shop.a):
        for read in (
            lambda: ledger.titles_for_source(actor=None, table_key=NOTE, record_id=order),
            lambda: ledger.settlements(actor=None, title_id=order),
            lambda: ledger.title(actor=None, title_id=order),
        ):
            with pytest.raises(Forbidden):
                read()
