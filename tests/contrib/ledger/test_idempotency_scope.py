"""Scoped idempotency and replay re-authorization in the ledger (#888).

Runs on PostgreSQL with the runtime role and SKELETON_BUNDLED_APPS=calendar,ledger.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import connection, connections
from ledger_consumer import NOTE, RULE

from skeleton.contrib.ledger.models import Book, Entry, RecurringTitle, Title
from skeleton.contrib.ledger.service_api import LedgerService, PostingLine
from skeleton.contrib.ledger.testing import account_id, enable_ledger
from skeleton.service_api import Conflict
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import acting_as, assert_service_contract, build_service_scenario
from tools.integration_database import disposable_database_pair

pytestmark = pytest.mark.needs_example_app
DAY = dt.date(2027, 6, 15)


@pytest.fixture
def two_books(skeleton_db):
    """Two private books in organization A: user_y owns Book A, user_x owns Book B."""
    from examples.notes.models import Note
    from skeleton.testing.applications import grant_table_actions
    from skeleton.testing.service_api import add_member, make_person

    scenario = build_service_scenario(Note, slug="ledger-idem")
    enable_ledger(scenario.a)
    grant_table_actions(
        scenario.a.pk,
        role_code="member",
        table_key="skeleton_ledger.book",
        actions=("read", "create", "update"),
    )
    grant_table_actions(
        scenario.a.pk,
        role_code="member",
        table_key=NOTE,
        actions=("read", "create", "update"),
    )
    user_y = make_person(email="owner-a@ledger-idem.example.test", password=scenario.password)
    user_x = make_person(email="owner-b@ledger-idem.example.test", password=scenario.password)
    for person in (user_y, user_x):
        add_member(organization=scenario.a, user=person, role_code="member")

    service = LedgerService()
    with acting_as(user_y, scenario.a) as actor_y:
        book_a = Book(
            organization=scenario.a,
            name="Livro A",
            currency="BRL",
            basis="cash",
            is_default=True,
            owner=actor_y,
            visibility="private",
        )
        book_a.save()
        service.apply_chart(actor=actor_y, book_id=book_a.pk)
        cash_a = account_id(actor_y, "1.1", book_id=book_a.pk)
        rev_a = account_id(actor_y, "4.1", book_id=book_a.pk)
        service.bind_key(actor=actor_y, key="example_notes.service_revenue", account_id=rev_a, book_id=book_a.pk)

    with acting_as(user_x, scenario.a) as actor_x:
        book_b = Book(
            organization=scenario.a,
            name="Livro B",
            currency="BRL",
            basis="cash",
            is_default=False,
            owner=actor_x,
            visibility="private",
        )
        book_b.save()
        service.apply_chart(actor=actor_x, book_id=book_b.pk)
        cash_b = account_id(actor_x, "1.1", book_id=book_b.pk)
        rev_b = account_id(actor_x, "4.1", book_id=book_b.pk)
        service.bind_key(actor=actor_x, key="example_notes.service_revenue", account_id=rev_b, book_id=book_b.pk)

    note_1 = scenario.create(scenario.a, owner=user_y, code="N-1", title="Nota 1", visibility="organization")
    note_2 = scenario.create(scenario.a, owner=user_y, code="N-2", title="Nota 2", visibility="organization")
    note_x = scenario.create(scenario.a, owner=user_x, code="N-X", title="Nota X", visibility="organization")
    return {
        "scenario": scenario,
        "user_y": user_y,
        "user_x": user_x,
        "book_a": book_a,
        "book_b": book_b,
        "cash_a": cash_a,
        "rev_a": rev_a,
        "cash_b": cash_b,
        "rev_b": rev_b,
        "note_1": note_1,
        "note_2": note_2,
        "note_x": note_x,
    }


def test_ledger_service_contract():
    assert_service_contract(LedgerService)


def test_idempotency_never_crosses_books_sources_or_entry_kinds(two_books, subtests):
    """#888: A/B books across post_manual, post, create_title, create_installments, create_recurrence, reverse."""
    ctx = two_books
    org = ctx["scenario"].a
    user_y, user_x = ctx["user_y"], ctx["user_x"]
    book_a, book_b = ctx["book_a"], ctx["book_b"]
    service = LedgerService()

    with subtests.test(case="post_manual"):
        with acting_as(user_y, org) as actor_y:
            entry_a = service.post_manual(
                actor=actor_y,
                book_id=book_a.pk,
                lines=(
                    PostingLine(account_id=ctx["cash_a"], debit=1000),
                    PostingLine(account_id=ctx["rev_a"], credit=1000),
                ),
                date=DAY,
                description="Manual A",
                idempotency_key="pm-1",
            )
            replay = service.post_manual(
                actor=actor_y,
                book_id=book_a.pk,
                lines=(
                    PostingLine(account_id=ctx["cash_a"], debit=1000),
                    PostingLine(account_id=ctx["rev_a"], credit=1000),
                ),
                date=DAY,
                description="Manual A",
                idempotency_key="pm-1",
            )
            assert replay.sysid == entry_a.sysid
        with acting_as(user_x, org) as actor_x:
            with pytest.raises(Conflict) as err:
                service.post_manual(
                    actor=actor_x,
                    book_id=book_b.pk,
                    lines=(
                        PostingLine(account_id=ctx["cash_b"], debit=1000),
                        PostingLine(account_id=ctx["rev_b"], credit=1000),
                    ),
                    date=DAY,
                    description="Manual B",
                    idempotency_key="pm-1",
                )
            assert err.value.code == "idempotency_conflict"
            assert "Manual A" not in str(err.value)
        with system_context(reason="test:idem-check-pm", organization_id=org.pk):
            assert Entry.objects.filter(book=book_b, idempotency_key="pm-1").count() == 0

    with subtests.test(case="post_rule"):
        with acting_as(user_y, org) as actor_y:
            posted_a = service.post(
                actor=actor_y,
                rule=RULE,
                amounts={"total": 900, "discount": 100, "gross": 1000},
                date=DAY,
                description="Atendimento N-1",
                source_id=ctx["note_1"].pk,
                idempotency_key="rule-1",
                accounts={"payment_account": ctx["cash_a"]},
                book_id=book_a.pk,
            )
            replay = service.post(
                actor=actor_y,
                rule=RULE,
                amounts={"total": 900, "discount": 100, "gross": 1000},
                date=DAY,
                description="Atendimento N-1",
                source_id=ctx["note_1"].pk,
                idempotency_key="rule-1",
                accounts={"payment_account": ctx["cash_a"]},
                book_id=book_a.pk,
            )
            assert replay.sysid == posted_a.sysid
            with pytest.raises(Conflict) as diff_src:
                service.post(
                    actor=actor_y,
                    rule=RULE,
                    amounts={"total": 900, "discount": 100, "gross": 1000},
                    date=DAY,
                    description="Atendimento N-2",
                    source_id=ctx["note_2"].pk,
                    idempotency_key="rule-1",
                    accounts={"payment_account": ctx["cash_a"]},
                    book_id=book_a.pk,
                )
            assert diff_src.value.code == "idempotency_conflict"
        with acting_as(user_x, org) as actor_x:
            with pytest.raises(Conflict) as diff_book:
                service.post(
                    actor=actor_x,
                    rule=RULE,
                    amounts={"total": 900, "discount": 100, "gross": 1000},
                    date=DAY,
                    description="Atendimento N-X",
                    source_id=ctx["note_x"].pk,
                    idempotency_key="rule-1",
                    accounts={"payment_account": ctx["cash_b"]},
                    book_id=book_b.pk,
                )
            assert diff_book.value.code == "idempotency_conflict"
        with acting_as(user_y, org) as actor_y:
            with pytest.raises(Conflict) as same_src_diff_book:
                service.post(
                    actor=actor_y,
                    rule=RULE,
                    amounts={"total": 900, "discount": 100, "gross": 1000},
                    date=DAY,
                    description="Atendimento N-1",
                    source_id=ctx["note_1"].pk,
                    idempotency_key="rule-1",
                    accounts={"payment_account": ctx["cash_b"]},
                    book_id=book_b.pk,
                )
            assert same_src_diff_book.value.code == "idempotency_conflict"

    with subtests.test(case="create_title"):
        with acting_as(user_y, org) as actor_y:
            title_a = service.create_title(
                actor=actor_y,
                book_id=book_a.pk,
                direction="receivable",
                amount=2500,
                due_date=DAY,
                description="Título A",
                counterparty_name="Contraparte Secreta A",
                category_key="ledger.revenue",
                idempotency_key="title-1",
            )
            assert (
                service.create_title(
                    actor=actor_y,
                    book_id=book_a.pk,
                    direction="receivable",
                    amount=2500,
                    due_date=DAY,
                    description="Título A",
                    category_key="ledger.revenue",
                    idempotency_key="title-1",
                ).sysid
                == title_a.sysid
            )
        with acting_as(user_x, org) as actor_x:
            with pytest.raises(Conflict) as err:
                service.create_title(
                    actor=actor_x,
                    book_id=book_b.pk,
                    direction="receivable",
                    amount=2500,
                    due_date=DAY,
                    description="Título B",
                    category_key="ledger.revenue",
                    idempotency_key="title-1",
                )
            assert err.value.code == "idempotency_conflict"
            assert "Contraparte Secreta A" not in str(err.value)
        with system_context(reason="test:idem-check-title", organization_id=org.pk):
            assert Title.objects.filter(book=book_b, idempotency_key="title-1").count() == 0

    with subtests.test(case="create_installments"):
        with acting_as(user_y, org) as actor_y:
            parts_a = service.create_installments(
                actor=actor_y,
                book_id=book_a.pk,
                direction="receivable",
                amount=3000,
                count=3,
                first_due_date=DAY,
                description="Parcelas A",
                category_key="ledger.revenue",
                idempotency_key="inst-1",
            )
            replay_parts = service.create_installments(
                actor=actor_y,
                book_id=book_a.pk,
                direction="receivable",
                amount=3000,
                count=3,
                first_due_date=DAY,
                description="Parcelas A",
                category_key="ledger.revenue",
                idempotency_key="inst-1",
            )
            assert [p.sysid for p in replay_parts] == [p.sysid for p in parts_a]
            with pytest.raises(Conflict) as single_err:
                service.create_title(
                    actor=actor_y,
                    book_id=book_a.pk,
                    direction="receivable",
                    amount=3000,
                    due_date=DAY,
                    description="Conflito",
                    category_key="ledger.revenue",
                    idempotency_key="inst-1",
                )
            assert single_err.value.code == "idempotency_conflict"
        with acting_as(user_x, org) as actor_x:
            with pytest.raises(Conflict) as err:
                service.create_installments(
                    actor=actor_x,
                    book_id=book_b.pk,
                    direction="receivable",
                    amount=3000,
                    count=3,
                    first_due_date=DAY,
                    description="Parcelas B",
                    category_key="ledger.revenue",
                    idempotency_key="inst-1",
                )
            assert err.value.code == "idempotency_conflict"
        with system_context(reason="test:idem-check-inst", organization_id=org.pk):
            assert Title.objects.filter(book=book_b, idempotency_key__startswith="inst-1#").count() == 0

    with subtests.test(case="create_recurrence"):
        with acting_as(user_y, org) as actor_y:
            rec_a = service.create_recurrence(
                actor=actor_y,
                book_id=book_a.pk,
                direction="payable",
                amount=5000,
                description="Aluguel A",
                counterparty_name="Locador Secreto A",
                category_key="ledger.expenses",
                frequency="monthly",
                start_date=DAY,
                idempotency_key="rec-1",
            )
            assert (
                service.create_recurrence(
                    actor=actor_y,
                    book_id=book_a.pk,
                    direction="payable",
                    amount=5000,
                    description="Aluguel A",
                    category_key="ledger.expenses",
                    frequency="monthly",
                    start_date=DAY,
                    idempotency_key="rec-1",
                ).sysid
                == rec_a.sysid
            )
        with acting_as(user_x, org) as actor_x:
            with pytest.raises(Conflict) as err:
                service.create_recurrence(
                    actor=actor_x,
                    book_id=book_b.pk,
                    direction="payable",
                    amount=5000,
                    description="Aluguel B",
                    category_key="ledger.expenses",
                    frequency="monthly",
                    start_date=DAY,
                    idempotency_key="rec-1",
                )
            assert err.value.code == "idempotency_conflict"
            assert "Locador Secreto A" not in str(err.value)
        with system_context(reason="test:idem-check-rec", organization_id=org.pk):
            assert RecurringTitle.objects.filter(book=book_b, idempotency_key="rec-1").count() == 0

    with subtests.test(case="reverse"):
        with acting_as(user_y, org) as actor_y:
            e1 = service.post_manual(
                actor=actor_y,
                book_id=book_a.pk,
                lines=(
                    PostingLine(account_id=ctx["cash_a"], debit=100),
                    PostingLine(account_id=ctx["rev_a"], credit=100),
                ),
                date=DAY,
                description="E1",
                idempotency_key="rev-e1",
            )
            e2 = service.post_manual(
                actor=actor_y,
                book_id=book_a.pk,
                lines=(
                    PostingLine(account_id=ctx["cash_a"], debit=200),
                    PostingLine(account_id=ctx["rev_a"], credit=200),
                ),
                date=DAY,
                description="E2",
                idempotency_key="rev-e2",
            )
            # Reusing another entry's key for a reversal must raise Conflict, not return E1.
            with pytest.raises(Conflict) as wrong_kind:
                service.reverse(actor=actor_y, entry_id=e2.sysid, date=DAY, idempotency_key="rev-e1")
            assert wrong_kind.value.code == "idempotency_conflict"
            rev_e2 = service.reverse(actor=actor_y, entry_id=e2.sysid, date=DAY, idempotency_key="rev-of-e2")
            assert rev_e2.reverses_id == e2.sysid
            assert (
                service.reverse(actor=actor_y, entry_id=e2.sysid, date=DAY, idempotency_key="rev-of-e2").sysid
                == rev_e2.sysid
            )
            # Reusing e2's reversal key when reversing e1 must raise Conflict.
            with pytest.raises(Conflict) as wrong_target:
                service.reverse(actor=actor_y, entry_id=e1.sysid, date=DAY, idempotency_key="rev-of-e2")
            assert wrong_target.value.code == "idempotency_conflict"


def test_concurrent_same_key_creation_produces_one_row_and_replays(two_books):
    """#888: two concurrent creations with the same tuple create one row and replay cleanly."""
    disposable_database_pair()
    assert connection.vendor == "postgresql"
    ctx = two_books
    org = ctx["scenario"].a
    user_y = ctx["user_y"]
    book_a = ctx["book_a"]
    barrier = Barrier(2)

    def create_once(_):
        connections.close_all()
        try:
            with acting_as(user_y, org) as actor_y:
                barrier.wait(10)
                return (
                    LedgerService()
                    .create_title(
                        actor=actor_y,
                        book_id=book_a.pk,
                        direction="receivable",
                        amount=1500,
                        due_date=DAY,
                        description="Concorrente",
                        category_key="ledger.revenue",
                        idempotency_key="race-title-1",
                    )
                    .sysid
                )
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create_once, range(2), timeout=60))
    assert len(set(ids)) == 1
    with system_context(reason="test:idem-race-read", organization_id=org.pk):
        assert Title.objects.filter(book=book_a, idempotency_key="race-title-1").count() == 1
