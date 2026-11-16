"""Posting rules authorized by the source record (#545): the notes of the example app are the source table."""

import datetime as dt
from uuid import uuid4

import pytest

from skeleton.contrib.ledger.registry import _rules
from skeleton.contrib.ledger.service_api import (
    AccountKey,
    Key,
    LedgerService,
    Param,
    PostingRule,
    RuleLine,
    register_account_key,
    register_posting_rule,
)
from skeleton.contrib.ledger.testing import account_id, enable_ledger, prepare_book
from skeleton.service_api import Conflict, Forbidden, InvalidInput, NotFound, Unavailable
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    build_service_scenario,
)

pytestmark = pytest.mark.needs_example_app
DAY = dt.date(2027, 4, 1)
NOTE = "example_notes.note"
RULE = "example_notes.service_received"
register_account_key(AccountKey(code="example_notes.service_revenue", label="Receita de serviços", kind="revenue"))
register_account_key(AccountKey(code="example_notes.unbound", label="Sem vínculo", kind="revenue"))
register_posting_rule(
    PostingRule(
        code=RULE,
        label="Serviço recebido",
        source_table_key=NOTE,
        source_action="update",
        lines=(
            RuleLine(side="debit", account=Param("payment_account"), amount="total"),
            RuleLine(side="debit", account=Key("ledger.discounts_given"), amount="discount"),
            RuleLine(side="credit", account=Key("example_notes.service_revenue"), amount="gross"),
        ),
    )
)
register_posting_rule(
    PostingRule(
        code="example_notes.unbound_rule",
        label="Sem vínculo",
        source_table_key=NOTE,
        source_action="update",
        lines=(
            RuleLine(side="debit", account=Param("payment_account"), amount="total"),
            RuleLine(side="credit", account=Key("example_notes.unbound"), amount="total"),
        ),
    )
)


@pytest.fixture
def ledger(skeleton_db):
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="ledger-rules")
    enable_ledger(scenario.a)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        prepare_book(admin, bindings={"example_notes.service_revenue": "4.1"})
        cash = account_id(admin, "1.1")
    note = scenario.create(scenario.a, code="AT-1", title="Atendimento 1", visibility="organization")
    return scenario, note, cash


def post(scenario, note, cash, *, user=None, key="k1", amounts=None, rule=RULE, accounts=None):
    return scenario.caller(
        lambda actor: LedgerService().post(
            actor=actor,
            rule=rule,
            amounts=amounts or {"total": 900, "discount": 100, "gross": 1000},
            date=DAY,
            description="Atendimento AT-1",
            source_id=note.pk,
            idempotency_key=key,
            accounts={"payment_account": cash} if accounts is None else accounts,
        ),
        user=user,
    )()


def test_the_rules_must_be_valid_at_registration():
    for bad in (
        PostingRule(
            "other_app.x",
            "x",
            NOTE,
            "update",
            (RuleLine("debit", Param("a"), "t"), RuleLine("credit", Param("b"), "t")),
        ),
        PostingRule(
            "example_notes.x",
            "x",
            NOTE,
            "update",
            (RuleLine("left", Param("a"), "t"), RuleLine("credit", Param("b"), "t")),
        ),
        PostingRule(
            "example_notes.x",
            "x",
            NOTE,
            "update",
            (RuleLine("debit", Param("a"), ""), RuleLine("credit", Param("b"), "t")),
        ),
        PostingRule(
            "example_notes.x",
            "x",
            NOTE,
            "fly",
            (RuleLine("debit", Param("a"), "t"), RuleLine("credit", Param("b"), "t")),
        ),
    ):
        with pytest.raises(ValueError):
            register_posting_rule(bad)


def test_the_check_reports_a_rule_with_an_unregistered_source_table():
    from skeleton.contrib.ledger.checks import posting_rule_checks

    assert posting_rule_checks() == []
    rule = PostingRule(
        "example_notes.ghost",
        "x",
        "example_notes.ghost",
        "update",
        (RuleLine("debit", Param("a"), "t"), RuleLine("credit", Param("b"), "t")),
    )
    register_posting_rule(rule)
    try:
        assert [error.id for error in posting_rule_checks()] == ["skeleton.E041"]
    finally:
        _rules.pop(rule.code)


def test_whoever_updates_the_source_posts_without_reading_the_book(ledger):
    scenario, note, cash = ledger
    entry = post(scenario, note, cash)
    assert entry.kind == "rule" and entry.rule == RULE and entry.total == 1000 and entry.source_id == note.pk
    assert sorted((line.debit, line.credit) for line in entry.lines) == [(0, 1000), (100, 0), (900, 0)]
    with pytest.raises(Forbidden):
        scenario.caller(lambda actor: LedgerService().books(actor=actor))()
    listed = scenario.caller(
        lambda actor: LedgerService().entries_for_source(actor=actor, table_key=NOTE, record_id=note.pk)
    )()
    assert [item.sysid for item in listed] == [entry.sysid]
    assert post(scenario, note, cash).sysid == entry.sysid  # Idempotent by (source app, key).


def test_without_update_on_the_source_or_reading_it(ledger):
    scenario, note, cash = ledger
    private = scenario.create(scenario.a, code="AT-2", title="Privado", owner=scenario.admin_a, visibility="private")
    with pytest.raises(NotFound):
        post(scenario, private, cash)
    with pytest.raises(NotFound):
        post(scenario, note, cash, user=scenario.denied)
    readable = scenario.create(
        scenario.a, code="AT-3", title="Leitura", owner=scenario.admin_a, visibility="organization"
    )
    with pytest.raises(Forbidden):
        post(scenario, readable, cash)


def test_keys_params_and_zero_components(ledger):
    scenario, note, cash = ledger
    with pytest.raises(Conflict) as error:
        post(scenario, note, cash, rule="example_notes.unbound_rule", amounts={"total": 10})
    assert error.value.code == "account_not_bound" and "example_notes.unbound" in error.value.detail
    with pytest.raises(InvalidInput):
        post(scenario, note, cash, accounts={})
    with pytest.raises(InvalidInput):
        post(scenario, note, cash, accounts={"payment_account": uuid4()})
    plain = post(scenario, note, cash, key="k2", amounts={"total": 1000, "discount": 0, "gross": 1000})
    assert len(plain.lines) == 2
    with pytest.raises(InvalidInput):
        post(scenario, note, cash, key="k3", amounts={"total": 1000, "discount": 0, "gross": 999})


def test_the_system_context_the_disabled_source_app_and_another_actor(ledger):
    scenario, note, cash = ledger
    with system_context(reason="test:ledger-job", organization_id=scenario.a.pk):
        entry = LedgerService().post(
            actor=None,
            rule=RULE,
            amounts={"total": 50, "gross": 50},
            date=DAY,
            description="Job",
            source_id=note.pk,
            idempotency_key="job-1",
            accounts={"payment_account": cash},
        )
    assert entry.total == 50
    with scenario.application_disabled():
        with pytest.raises(Unavailable):
            post(scenario, note, cash, key="k4")
    assert_rejects_other_actor(
        scenario.caller(
            lambda actor: LedgerService().entries_for_source(actor=scenario.admin_a, table_key=NOTE, record_id=note.pk)
        )
    )
    assert_service_contract(LedgerService)


def test_the_source_reverses_its_entry_with_cas(ledger):
    scenario, note, cash = ledger
    entry = post(scenario, note, cash)
    with pytest.raises(Conflict) as stale:
        scenario.caller(
            lambda actor: LedgerService().reverse(actor=actor, entry_id=entry.sysid, date=DAY, expected_revision=1)
        )()
    assert stale.value.code == "stale_revision"
    reversal = scenario.caller(
        lambda actor: LedgerService().reverse(actor=actor, entry_id=entry.sysid, date=DAY, expected_revision=2)
    )()
    assert reversal.reverses_id == entry.sysid


def test_a_title_with_a_source_is_authorized_by_the_source(ledger):
    scenario, note, cash = ledger
    created = scenario.caller(
        lambda actor: LedgerService().create_title(
            actor=actor,
            direction="receivable",
            amount=500,
            due_date=DAY,
            issue_date=DAY,
            description="Atendimento AT-1",
            category_key="example_notes.service_revenue",
            idempotency_key="title-1",
            source_table=NOTE,
            source_id=note.pk,
            counterparty_name="Cliente Exemplo",
        )
    )()
    settled = scenario.caller(
        lambda actor: LedgerService().settle_title(
            actor=actor,
            title_id=created.sysid,
            amount=500,
            date=DAY,
            payment_account_id=cash,
            method="cash",
            expected_revision=created.revision,
            idempotency_key="pay-1",
        )
    )()
    assert settled.amount == 500
    listed = scenario.caller(
        lambda actor: LedgerService().titles_for_source(actor=actor, table_key=NOTE, record_id=note.pk)
    )()
    assert [item.status for item in listed] == ["settled"] and listed[0].counterparty_name == "Cliente Exemplo"
    with pytest.raises(NotFound):  # The book itself stays out of reach.
        scenario.caller(lambda actor: LedgerService().titles(actor=actor))()
    readable = scenario.create(
        scenario.a, code="AT-9", title="Só leitura", owner=scenario.admin_a, visibility="organization"
    )
    with pytest.raises(Forbidden):
        scenario.caller(
            lambda actor: LedgerService().create_title(
                actor=actor,
                direction="receivable",
                amount=1,
                due_date=DAY,
                description="x",
                category_key="ledger.revenue",
                idempotency_key="title-2",
                source_table=NOTE,
                source_id=readable.pk,
            )
        )()
