"""Balances and reports (#548): each one against an independent calculation over a fixed sample book."""

import datetime as dt
from collections import defaultdict

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from skeleton.contrib.ledger import reports
from skeleton.contrib.ledger.service_api import LedgerService, PostingLine
from skeleton.service_api import Conflict, InvalidInput, NotFound
from skeleton.testing.service_api import acting_as

d = dt.date

# (date, description, ((code, debit, credit), ...)): the sample book, cash basis.
SAMPLE = (
    (d(2027, 1, 5), "Abertura", (("1.2", 100000, 0), ("3.1", 0, 100000))),
    (d(2027, 1, 10), "Serviço à vista", (("1.1", 30000, 0), ("4.1", 0, 30000))),
    (d(2027, 2, 3), "Despesa paga", (("5.1", 12000, 0), ("1.2", 0, 12000))),
    (d(2027, 2, 20), "Serviço no banco", (("1.2", 20000, 0), ("5.4", 500, 0), ("4.1", 0, 20500))),
    (d(2027, 3, 15), "Juros", (("1.1", 1000, 0), ("4.2", 0, 1000))),
)
DEBIT_NATURE = {"1", "5"}  # Assets and expenses, by the first digit of the code.


def test_aging_buckets_and_code_order():
    as_of = d(2027, 4, 15)
    cases = {d(2027, 4, 15): "current", d(2027, 4, 14): "1-30", d(2027, 3, 16): "1-30", d(2027, 3, 15): "31-60"}
    cases |= {d(2027, 2, 14): "31-60", d(2027, 2, 13): "61-90", d(2027, 1, 15): "61-90", d(2027, 1, 14): "90+"}
    for due, bucket in cases.items():
        assert reports.aging_bucket(due, as_of) == bucket, due
    assert sorted(["1.10", "1.2", "1", "2", "1.1"], key=reports.code_key) == ["1", "1.1", "1.2", "1.10", "2"]


def call(scenario, operation, organization=None, **kwargs):
    return scenario.caller(
        lambda actor: getattr(LedgerService(), operation)(actor=actor, **kwargs), organization=organization
    )()


@pytest.fixture
def sample(scenario):
    """A's default book with the services chart and the SAMPLE entries; account ids by code."""
    call(scenario, "create_default_book")
    call(scenario, "apply_chart")
    codes = {item.code: item.sysid for item in call(scenario, "accounts", page_size=100).items}
    for number, (day, description, lines) in enumerate(SAMPLE):
        call(
            scenario,
            "post_manual",
            lines=tuple(PostingLine(codes[code], debit=debit, credit=credit) for code, debit, credit in lines),
            date=day,
            description=description,
            idempotency_key=f"sample-{number}",
        )
    return codes


def expected(start, end):
    """Independent totals per analytic code: opening (before start) and movement in the window."""
    opening, debit, credit = defaultdict(int), defaultdict(int), defaultdict(int)
    for day, _, lines in SAMPLE:
        for code, line_debit, line_credit in lines:
            sign = 1 if code[0] in DEBIT_NATURE else -1
            if day < start:
                opening[code] += sign * (line_debit - line_credit)
            elif day <= end:
                debit[code] += line_debit
                credit[code] += line_credit
    return opening, debit, credit


def test_trial_balance_matches_an_independent_calculation(scenario, sample):
    start, end = d(2027, 2, 1), d(2027, 2, 28)
    trial = call(scenario, "trial_balance", start=start, end=end)
    opening, debit, credit = expected(start, end)
    rows = {row.code: row for row in trial.rows}
    assert trial.total_debit == trial.total_credit == 32500
    for code, row in rows.items():
        if row.is_group:
            continue
        sign = 1 if code[0] in DEBIT_NATURE else -1
        assert (row.opening, row.debit, row.credit) == (opening[code], debit[code], credit[code]), code
        assert row.closing == row.opening + sign * (row.debit - row.credit), code
    # Groups sum their children.
    assert rows["1"].closing == rows["1.1"].closing + rows["1.2"].closing + rows["1.3"].closing == 138000
    assert rows["4"].debit == 0 and rows["4"].credit == 20500
    assert [row.code for row in trial.rows][:4] == ["1", "1.1", "1.2", "1.3"]


def test_balance_by_id_by_key_and_of_a_group(scenario, sample):
    bank = call(scenario, "balance", as_of=d(2027, 2, 10), account_id=sample["1.2"])
    assert (bank.debit, bank.credit, bank.balance) == (100000, 12000, 88000)
    assert call(scenario, "balance", as_of=d(2027, 12, 31), account_key="ledger.bank").balance == 108000
    assets = call(scenario, "balance", as_of=d(2027, 12, 31), account_id=sample["1"])
    assert assets.is_group and assets.balance == 139000
    with pytest.raises(InvalidInput):
        call(scenario, "balance", as_of=d(2027, 1, 1))
    with pytest.raises(NotFound):
        call(scenario, "balance", as_of=d(2027, 1, 1), account_id=scenario.a.pk)
    from skeleton.contrib.ledger.registry import AccountKey, register_account_key

    register_account_key(AccountKey(code="ledger_tests.report_unbound", label="x", kind="asset"))
    with pytest.raises(Conflict):
        call(scenario, "balance", as_of=d(2027, 1, 1), account_key="ledger_tests.report_unbound")


def test_income_statement_is_revenue_minus_expenses(scenario, sample):
    statement = call(scenario, "income_statement", start=d(2027, 1, 1), end=d(2027, 3, 31))
    revenue = sum(credit - debit for _, _, lines in SAMPLE for code, debit, credit in lines if code[0] == "4")
    expense = sum(debit - credit for _, _, lines in SAMPLE for code, debit, credit in lines if code[0] == "5")
    assert (statement.revenue, statement.expense, statement.result) == (revenue, expense, revenue - expense)
    assert (revenue, expense) == (51500, 12500)
    assert {row.code for row in statement.rows} >= {"4", "4.1", "5", "5.4"}
    assert next(row for row in statement.rows if row.code == "4").closing == revenue


def test_account_statement_is_paginated_with_a_running_balance(scenario, sample):
    first = call(
        scenario, "account_statement", account_id=sample["1.2"], start=d(2027, 1, 1), end=d(2027, 3, 31), page_size=2
    )
    assert first.opening == 0
    assert [(line.date, line.balance) for line in first.page.items] == [(d(2027, 1, 5), 100000), (d(2027, 2, 3), 88000)]
    second = call(
        scenario,
        "account_statement",
        account_id=sample["1.2"],
        start=d(2027, 1, 1),
        end=d(2027, 3, 31),
        page_size=2,
        cursor=first.page.next_cursor,
    )
    assert [(line.date, line.balance) for line in second.page.items] == [(d(2027, 2, 20), 108000)]
    assert second.page.next_cursor is None
    later = call(scenario, "account_statement", account_id=sample["1.2"], start=d(2027, 2, 4), end=d(2027, 3, 31))
    assert later.opening == 88000 and [line.balance for line in later.page.items] == [108000]
    group = call(scenario, "account_statement", account_id=sample["1"], start=d(2027, 1, 1), end=d(2027, 12, 31))
    assert [line.balance for line in group.page.items] == [100000, 130000, 118000, 138000, 139000]
    assert [line.number for line in group.page.items] == [1, 2, 3, 4, 5]


def test_cash_flow_realized_and_forecast(scenario, sample):
    for key, direction, amount, due in (
        ("f1", "receivable", 4000, d(2027, 4, 20)),
        ("f2", "payable", 700, d(2027, 4, 10)),
        ("f3", "receivable", 900, d(2027, 6, 1)),  # Outside the window.
    ):
        call(
            scenario,
            "create_title",
            direction=direction,
            amount=amount,
            due_date=due,
            issue_date=d(2027, 1, 1),
            description="Previsto",
            category_key="ledger.revenue" if direction == "receivable" else "ledger.expenses",
            idempotency_key=key,
        )
    flow = call(scenario, "cash_flow", start=d(2027, 2, 1), end=d(2027, 4, 30))
    assert flow.opening == 130000  # Cash and bank before February.
    assert (flow.realized_in, flow.realized_out, flow.closing) == (21000, 12000, 139000)
    assert (flow.forecast_in, flow.forecast_out) == (4000, 700)
    assert [(month.month, month.realized_in, month.realized_out) for month in flow.months] == [
        (d(2027, 2, 1), 20000, 12000),
        (d(2027, 3, 1), 1000, 0),
        (d(2027, 4, 1), 0, 0),
    ]
    assert flow.months[-1].forecast_in == 4000


def test_aging_by_overdue_bucket(scenario, sample):
    dues = (d(2027, 4, 20), d(2027, 4, 1), d(2027, 3, 1), d(2027, 1, 20), d(2026, 12, 1))
    for number, due in enumerate(dues):
        call(
            scenario,
            "create_title",
            direction="receivable",
            amount=1000 * (number + 1),
            due_date=due,
            issue_date=d(2026, 11, 1),
            description="Atraso",
            category_key="ledger.revenue",
            idempotency_key=f"a{number}",
        )
    found = call(scenario, "aging", as_of=d(2027, 4, 15))
    assert [(bucket.name, bucket.amount, bucket.count) for bucket in found.buckets] == [
        ("current", 1000, 1),
        ("1-30", 2000, 1),
        ("31-60", 3000, 1),
        ("61-90", 4000, 1),
        ("90+", 5000, 1),
    ]
    assert found.total == 15000
    assert call(scenario, "aging", as_of=d(2027, 4, 15), direction="payable").total == 0


def test_an_unsealed_entry_never_counts(scenario, sample):
    from skeleton.contrib.ledger.models import Book, Entry, EntryLine

    before = call(scenario, "trial_balance", start=d(2027, 1, 1), end=d(2027, 12, 31))
    with acting_as(scenario.member, scenario.a):
        book = Book.objects.get(is_default=True)
        entry = Entry(
            organization_id=scenario.a.pk,
            book=book,
            year=2027,
            number=99,
            date=d(2027, 2, 1),
            description="x",
            kind="manual",
            total=5000,
        )
        entry.save()
        for code, debit, credit in (("1.1", 5000, 0), ("4.1", 0, 5000)):
            EntryLine(
                organization_id=scenario.a.pk,
                entry=entry,
                account_id=sample[code],
                date=entry.date,
                debit=debit,
                credit=credit,
            ).save()
    after = call(scenario, "trial_balance", start=d(2027, 1, 1), end=d(2027, 12, 31))
    assert after == before
    assert call(scenario, "balance", as_of=d(2027, 12, 31), account_id=sample["1.1"]).balance == 31000
    statement = call(scenario, "account_statement", account_id=sample["1.1"], start=d(2027, 1, 1), end=d(2027, 12, 31))
    assert len(statement.page.items) == 2
    assert call(scenario, "income_statement", start=d(2027, 1, 1), end=d(2027, 12, 31)).revenue == 51500


def test_each_report_runs_a_fixed_number_of_queries(scenario, sample):
    reports_to_run = {
        "trial_balance": {"start": d(2027, 1, 1), "end": d(2027, 12, 31)},
        "income_statement": {"start": d(2027, 1, 1), "end": d(2027, 12, 31)},
        "cash_flow": {"start": d(2027, 1, 1), "end": d(2027, 12, 31)},
        "aging": {"as_of": d(2027, 12, 31)},
        "balance": {"as_of": d(2027, 12, 31), "account_key": "ledger.bank"},
        "account_statement": {"account_id": sample["1"], "start": d(2027, 1, 1), "end": d(2027, 12, 31)},
    }

    def counts():
        found = {}
        for name, kwargs in reports_to_run.items():
            with CaptureQueriesContext(connection) as queries:
                call(scenario, name, **kwargs)
            found[name] = len(queries)
        return found

    first = counts()
    for number in range(6):
        call(
            scenario,
            "post_manual",
            lines=(PostingLine(sample["1.1"], debit=100), PostingLine(sample["4.1"], credit=100)),
            date=d(2027, 5, 1 + number),
            description="Mais",
            idempotency_key=f"more-{number}",
        )
    assert counts() == first


def test_windows_readers_and_tenants(scenario, sample):
    with pytest.raises(InvalidInput):
        call(scenario, "trial_balance", start=d(2020, 1, 1), end=d(2027, 1, 2))
    with pytest.raises(InvalidInput):
        call(scenario, "cash_flow", start=d(2027, 2, 1), end=d(2027, 1, 1))
    with pytest.raises(InvalidInput):
        call(scenario, "aging", as_of=d(2027, 1, 1), direction="both")
    [book] = call(scenario, "books")
    with pytest.raises(NotFound):
        call(
            scenario,
            "trial_balance",
            organization=scenario.b,
            start=d(2027, 1, 1),
            end=d(2027, 1, 2),
            book_id=book.sysid,
        )
    with scenario.as_denied() as actor, pytest.raises(NotFound):
        LedgerService().income_statement(actor=actor, start=d(2027, 1, 1), end=d(2027, 1, 2))
