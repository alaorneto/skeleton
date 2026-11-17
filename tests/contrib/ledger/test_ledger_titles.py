"""Receivables, payables and settlements (#546): the entries of each basis, balances, CAS and constraints."""

import datetime as dt

import pytest
from django.db import IntegrityError, connection, transaction

from skeleton.contrib.ledger.service_api import LedgerService
from skeleton.service_api import Conflict, InvalidInput
from skeleton.testing.service_api import acting_as

DAY = dt.date(2027, 5, 10)


def call(scenario, operation, organization=None, **kwargs):
    return scenario.caller(
        lambda actor: getattr(LedgerService(), operation)(actor=actor, **kwargs), organization=organization
    )()


@pytest.fixture
def books(scenario):
    """A cash book in A and an accrual book in B, both with the services chart; account codes by id."""
    codes = {}
    for organization, basis in ((scenario.a, "cash"), (scenario.b, "accrual")):
        call(scenario, "create_default_book", organization=organization, basis=basis)
        call(scenario, "apply_chart", organization=organization)
        page = call(scenario, "accounts", organization=organization, page_size=100)
        codes[organization.pk] = {item.code: item.sysid for item in page.items}
    return codes


def entry_codes(scenario, organization, entry_id, codes):
    names = {value: key for key, value in codes[organization.pk].items()}
    entry = call(scenario, "entry", organization=organization, entry_id=entry_id)
    return sorted((names[line.account_id], line.debit, line.credit) for line in entry.lines)


def title(scenario, organization, direction, key, **changes):
    values = {
        "direction": direction,
        "amount": 1000,
        "due_date": DAY,
        "issue_date": DAY,
        "description": "Serviço",
        "category_key": "ledger.revenue" if direction == "receivable" else "ledger.expenses",
        "idempotency_key": key,
        "counterparty_name": "Maria Exemplo",
        **changes,
    }
    return call(scenario, "create_title", organization=organization, **values)


def settle(scenario, organization, created, codes, key="s", **changes):
    values = {
        "title_id": created.sysid,
        "amount": created.balance,
        "date": DAY,
        "payment_account_id": codes[organization.pk]["1.1"],
        "method": "pix",
        "expected_revision": created.revision,
        "idempotency_key": key,
        "discount": 50,
        "interest": 30,
        "fee": 20,
        **changes,
    }
    return call(scenario, "settle_title", organization=organization, **values)


def test_cash_basis_receivable_and_payable(scenario, books):
    a = scenario.a
    receivable = title(scenario, a, "receivable", "r1")
    assert not call(scenario, "entries", start=DAY, end=DAY).items  # Nothing on creation in cash basis.
    received = settle(scenario, a, receivable, books)
    assert entry_codes(scenario, a, received.entry_id, books) == sorted(
        [("1.1", 960, 0), ("5.2", 50, 0), ("5.4", 20, 0), ("4.1", 0, 1000), ("4.2", 0, 30)]
    )
    payable = title(scenario, a, "payable", "p1")
    paid = settle(scenario, a, payable, books, key="p")
    assert entry_codes(scenario, a, paid.entry_id, books) == sorted(
        [("5.1", 1000, 0), ("5.3", 30, 0), ("5.4", 20, 0), ("1.1", 0, 1000), ("4.3", 0, 50)]
    )


def test_accrual_basis_recognizes_on_creation_and_settles_against_receivables_and_payables(scenario, books):
    b = scenario.b
    receivable = title(scenario, b, "receivable", "r1")
    [recognition] = call(scenario, "entries", organization=b, start=DAY, end=DAY).items
    assert entry_codes(scenario, b, recognition.sysid, books) == [("1.3", 1000, 0), ("4.1", 0, 1000)]
    received = settle(scenario, b, receivable, books)
    assert entry_codes(scenario, b, received.entry_id, books) == sorted(
        [("1.1", 960, 0), ("5.2", 50, 0), ("5.4", 20, 0), ("1.3", 0, 1000), ("4.2", 0, 30)]
    )
    payable = title(scenario, b, "payable", "p1")
    recognitions = call(scenario, "entries", organization=b, start=DAY, end=DAY).items
    payable_entry = next(item for item in recognitions if item.kind == "accrual" and item.sysid != recognition.sysid)
    assert entry_codes(scenario, b, payable_entry.sysid, books) == [("2.1", 0, 1000), ("5.1", 1000, 0)]
    paid = settle(scenario, b, payable, books, key="p")
    assert entry_codes(scenario, b, paid.entry_id, books) == sorted(
        [("2.1", 1000, 0), ("5.3", 30, 0), ("5.4", 20, 0), ("1.1", 0, 1000), ("4.3", 0, 50)]
    )


def test_partial_settlements_and_a_settlement_above_the_balance(scenario, books):
    a = scenario.a
    created = title(scenario, a, "receivable", "t1")
    settle(scenario, a, created, books, amount=400, discount=0, interest=0, fee=0)
    partial = call(scenario, "titles").items[0]
    assert (partial.status, partial.amount_settled, partial.balance) == ("partial", 400, 600)
    with pytest.raises(InvalidInput):
        settle(scenario, a, partial, books, key="s2", amount=700, discount=0, interest=0, fee=0)
    settle(scenario, a, partial, books, key="s3", amount=600, discount=0, interest=0, fee=0)
    assert call(scenario, "titles").items[0].status == "settled"
    with pytest.raises(Conflict) as error:
        settle(scenario, a, call(scenario, "titles").items[0], books, key="s4", amount=1)
    assert error.value.code == "title_settled"


def test_two_settlements_with_the_same_revision_have_one_winner(scenario, books):
    a = scenario.a
    created = title(scenario, a, "receivable", "c1")
    settle(scenario, a, created, books, key="w1", discount=0, interest=0, fee=0)
    with pytest.raises(Conflict) as error:
        settle(scenario, a, created, books, key="w2", discount=0, interest=0, fee=0)
    assert error.value.code == "stale_revision"
    assert call(scenario, "titles").items[0].amount_settled == 1000


def test_reversing_a_settlement_gives_the_balance_back_and_keeps_the_row(scenario, books):
    a = scenario.a
    created = title(scenario, a, "receivable", "v1")
    settled = settle(scenario, a, created, books, discount=0, interest=0, fee=0)
    current = call(scenario, "titles").items[0]
    reopened = call(
        scenario, "reverse_settlement", settlement_id=settled.sysid, date=DAY, expected_revision=current.revision
    )
    assert (reopened.status, reopened.balance) == ("open", 1000)
    [row] = call(scenario, "settlements", title_id=created.sysid)
    assert row.reversal_entry_id is not None
    with pytest.raises(Conflict):
        call(scenario, "reverse_settlement", settlement_id=settled.sysid, date=DAY, expected_revision=reopened.revision)


def test_cancelling_needs_no_live_settlement_and_reverses_the_recognition(scenario, books):
    a, b = scenario.a, scenario.b
    with_settlement = title(scenario, a, "receivable", "x1")
    settle(scenario, a, with_settlement, books, amount=100, discount=0, interest=0, fee=0)
    current = call(scenario, "titles").items[0]
    with pytest.raises(Conflict) as error:
        call(scenario, "cancel_title", title_id=current.sysid, expected_revision=current.revision)
    assert error.value.code == "title_has_settlements"
    accrual = title(scenario, b, "receivable", "x2")
    cancelled = call(
        scenario, "cancel_title", organization=b, title_id=accrual.sysid, expected_revision=accrual.revision, date=DAY
    )
    assert cancelled.status == "cancelled"
    kinds = sorted(item.kind for item in call(scenario, "entries", organization=b, start=DAY, end=DAY).items)
    assert kinds == ["accrual", "reversal"]


def test_installments_sum_to_the_total_and_respect_the_end_of_the_month(scenario, books):
    parts = call(
        scenario,
        "create_installments",
        direction="receivable",
        amount=1000,
        count=3,
        first_due_date=dt.date(2027, 1, 31),
        description="Pacote",
        category_key="ledger.revenue",
        idempotency_key="parc",
    )
    assert [part.amount for part in parts] == [333, 333, 334]
    assert [part.due_date for part in parts] == [dt.date(2027, 1, 31), dt.date(2027, 2, 28), dt.date(2027, 3, 31)]
    assert [part.installment_number for part in parts] == [1, 2, 3]
    again = call(
        scenario,
        "create_installments",
        direction="receivable",
        amount=1000,
        count=3,
        first_due_date=dt.date(2027, 1, 31),
        description="Pacote",
        category_key="ledger.revenue",
        idempotency_key="parc",
    )
    assert [part.sysid for part in again] == [part.sysid for part in parts]


def test_the_counterparty_name_is_redacted_in_the_audit(scenario, books):
    from skeleton.audit.models import AuditEvent

    created = title(scenario, scenario.a, "receivable", "n1")
    with acting_as(scenario.admin_a, scenario.a):
        event = AuditEvent.objects.get(record_sysid=created.sysid, operation="create")
    assert event.changes["counterparty_name"]["new"] == "[REDACTED]"
    assert "Maria Exemplo" not in str(event.changes)


def test_the_value_and_status_constraints_hold_by_sql(scenario, books):
    created = title(scenario, scenario.a, "receivable", "q1")
    with acting_as(scenario.member, scenario.a), connection.cursor() as cursor:
        for assignment in ("amount_settled=2000", "status='settled'", "amount_settled=10, status='open'"):
            with pytest.raises(IntegrityError), transaction.atomic():
                cursor.execute(
                    f"UPDATE skeleton.skeleton_ledger_title SET {assignment}, revision=revision+1 WHERE sysid=%s",
                    [created.sysid],
                )


def test_a_category_key_without_binding_is_a_conflict(scenario, books):
    from skeleton.contrib.ledger.registry import AccountKey, register_account_key

    register_account_key(AccountKey(code="ledger_tests.unbound_category", label="x", kind="revenue"))
    with pytest.raises(Conflict) as error:
        title(scenario, scenario.a, "receivable", "u1", category_key="ledger_tests.unbound_category")
    assert error.value.code == "account_not_bound"
