"""A derived table as the source of a posting rule and of a title (#565).

The source is ``WorkItem``, authorized through its ``Workspace`` (docs/access-control.md,
"Tabelas derivadas"). The rule is registered only while the tables exist, so the E041 check of
the other tests keeps passing.
"""

import datetime as dt

import pytest
from ledger_consumer import NOTE  # Registers the consumer's account keys once.

from skeleton.contrib.ledger.checks import posting_rule_checks
from skeleton.contrib.ledger.registry import _rules
from skeleton.contrib.ledger.service_api import LedgerService, Param, PostingRule, RuleLine, register_posting_rule
from skeleton.contrib.ledger.testing import account_id, enable_ledger, prepare_book
from skeleton.service_api import Forbidden, NotFound
from skeleton.testing.service_api import acting_as, build_service_scenario
from tests.integration.derived_support import child, derived_tables, grant

pytestmark = pytest.mark.needs_example_app
ITEM = "example_notes.workitem"
LINE = "example_notes.workitemline"
RULE = "example_notes.item_received"
DAY = dt.date(2027, 5, 1)


def rule(source=ITEM, code=RULE):
    return PostingRule(
        code=code,
        label="Item recebido",
        source_table_key=source,
        source_action="update",
        lines=(
            RuleLine(side="debit", account=Param("payment_account"), amount="total"),
            RuleLine(side="credit", account=Param("revenue_account"), amount="total"),
        ),
    )


@pytest.fixture
def world(skeleton_db, owner_schema_editor):
    assert NOTE  # The consumer module is imported for its keys.
    with derived_tables(owner_schema_editor) as tables:
        register_posting_rule(rule())
        try:
            scenario = build_service_scenario(tables.workspace, slug="ledger-derived")
            enable_ledger(scenario.a)
            with acting_as(scenario.admin_a, scenario.a) as admin:
                prepare_book(admin, bindings={"example_notes.service_revenue": "4.1"})
                accounts = {"payment_account": account_id(admin, "1.1"), "revenue_account": account_id(admin, "4.1")}
            admin = scenario.admin_a
            spaces = {
                "writable": scenario.create(scenario.a, owner=admin, visibility="private", title="writable"),
                "readable": scenario.create(scenario.a, owner=admin, visibility="organization", title="readable"),
                "hidden": scenario.create(scenario.a, owner=admin, visibility="private", title="hidden"),
            }
            grant(scenario, spaces["writable"], user=scenario.member, action="read")
            grant(scenario, spaces["writable"], user=scenario.member, action="update")
            items = {
                name: child(tables.item, scenario.a, workspace=space, title=name) for name, space in spaces.items()
            }
            yield tables, scenario, items, accounts
        finally:
            _rules.pop(RULE, None)


def post(scenario, item, accounts, *, key="d1", user=None):
    return scenario.caller(
        lambda actor: LedgerService().post(
            actor=actor,
            rule=RULE,
            amounts={"total": 700},
            date=DAY,
            description="Item",
            source_id=item.pk,
            idempotency_key=key,
            accounts=accounts,
        ),
        user=user,
    )()


def test_the_check_accepts_a_derived_source_and_still_refuses_an_unauthorizable_one(world):
    assert posting_rule_checks() == []
    bad = rule(source="example_notes.workitemghost", code="example_notes.item_ghost")
    register_posting_rule(bad)
    try:
        assert [error.id for error in posting_rule_checks()] == ["skeleton.E041"]
    finally:
        _rules.pop(bad.code)


def test_posting_from_a_child_follows_the_anchor(world):
    tables, scenario, items, accounts = world
    entry = post(scenario, items["writable"], accounts)
    assert entry.source_id == items["writable"].pk and entry.source_table == ITEM and entry.total == 700
    assert post(scenario, items["writable"], accounts).sysid == entry.sysid  # Idempotent, as before.
    with pytest.raises(Forbidden):  # Reads the anchor, cannot update it.
        post(scenario, items["readable"], accounts, key="d2")
    with pytest.raises(NotFound):
        post(scenario, items["hidden"], accounts, key="d3")
    with pytest.raises(NotFound):  # No table permission on the anchor.
        post(scenario, items["writable"], accounts, key="d4", user=scenario.denied)
    listed = scenario.caller(
        lambda actor: LedgerService().entries_for_source(actor=actor, table_key=ITEM, record_id=items["writable"].pk)
    )()
    assert [row.sysid for row in listed] == [entry.sysid]
    with pytest.raises(NotFound):
        scenario.caller(
            lambda actor: LedgerService().entries_for_source(
                actor=actor, table_key=ITEM, record_id=items["writable"].pk
            ),
            user=scenario.denied,
        )()


def test_a_title_with_a_derived_source(world):
    tables, scenario, items, accounts = world

    def create(item, key):
        return scenario.caller(
            lambda actor: LedgerService().create_title(
                actor=actor,
                direction="receivable",
                amount=300,
                due_date=DAY,
                issue_date=DAY,
                description="Item",
                category_key="example_notes.service_revenue",
                idempotency_key=key,
                source_table=ITEM,
                source_id=item.pk,
            )
        )()

    created = create(items["writable"], "t1")
    listed = scenario.caller(
        lambda actor: LedgerService().titles_for_source(actor=actor, table_key=ITEM, record_id=items["writable"].pk)
    )()
    assert [row.sysid for row in listed] == [created.sysid]
    with pytest.raises(Forbidden):
        create(items["readable"], "t2")
    with pytest.raises(NotFound):
        scenario.caller(
            lambda actor: LedgerService().titles_for_source(actor=actor, table_key=ITEM, record_id=items["hidden"].pk)
        )()
