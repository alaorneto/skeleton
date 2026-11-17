"""Sealed double-entry postings (#544): numbering, refusals by the service and by the database, reversal, lock."""

import datetime as dt

import pytest
from django.db import DatabaseError, connection, transaction

from skeleton.common.exceptions import LiveReferencesExist, RecordSealed
from skeleton.contrib.ledger.models import Entry, EntryLine
from skeleton.contrib.ledger.service_api import LedgerService, PostingLine
from skeleton.service_api import Conflict, InvalidInput, NotFound
from skeleton.testing.service_api import acting_as, assert_tenant_isolated

DAY = dt.date(2027, 3, 10)


def call(scenario, method, user=None, organization=None, **kwargs):
    return scenario.caller(
        lambda actor: getattr(LedgerService(), method)(actor=actor, **kwargs), user=user, organization=organization
    )()


@pytest.fixture
def accounts(scenario, book):
    call(scenario, "apply_chart")
    return {item.code: item.sysid for item in call(scenario, "accounts", page_size=100).items}


def lines(accounts, amount=1000, debit="1.1", credit="4.1"):
    return (
        PostingLine(account_id=accounts[debit], debit=amount),
        PostingLine(account_id=accounts[credit], credit=amount),
    )


def post(scenario, accounts, key, **changes):
    values = {"lines": lines(accounts), "date": DAY, "description": "Serviço", "idempotency_key": key, **changes}
    return call(scenario, "post_manual", **values)


def test_a_balanced_entry_is_numbered_and_sealed(scenario, accounts):
    first, second = post(scenario, accounts, "e1"), post(scenario, accounts, "e2")
    assert first.sealed and len(first.digest) == 64 and first.total == 1000
    assert (first.year, first.number, second.number) == (2027, 1, 2)
    assert sorted((line.debit, line.credit) for line in first.lines) == [(0, 1000), (1000, 0)]
    assert post(scenario, accounts, "e1").sysid == first.sysid  # Idempotent.
    other_year = post(scenario, accounts, "e3", date=dt.date(2028, 1, 2))
    assert other_year.number == 1


def test_a_rollback_gives_the_number_back(scenario, accounts):
    post(scenario, accounts, "a1")
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(RuntimeError), transaction.atomic():
            LedgerService().post_manual(
                actor=actor, lines=lines(accounts), date=DAY, description="x", idempotency_key="a2"
            )
            raise RuntimeError
    assert post(scenario, accounts, "a3").number == 2


@pytest.mark.parametrize(
    "bad",
    [
        lambda a: (PostingLine(account_id=a["1.1"], debit=1000), PostingLine(account_id=a["4.1"], credit=900)),
        lambda a: (PostingLine(account_id=a["1.1"], debit=1000),),
        lambda a: (PostingLine(account_id=a["1"], debit=1000), PostingLine(account_id=a["4.1"], credit=1000)),
        lambda a: (PostingLine(account_id=a["1.1"], debit=10, credit=10), PostingLine(account_id=a["4.1"], credit=0)),
    ],
)
def test_the_service_refuses_lines_that_do_not_balance_or_are_not_valid(scenario, accounts, bad):
    with pytest.raises(InvalidInput):
        post(scenario, accounts, "bad", lines=bad(accounts))


def test_the_database_refuses_to_seal_lines_that_do_not_balance(scenario, accounts):
    from django.utils import timezone

    book_id = call(scenario, "books")[0].sysid
    with acting_as(scenario.member, scenario.a):
        entry = Entry(
            organization_id=scenario.a.pk,
            book_id=book_id,
            year=2027,
            number=99,
            date=DAY,
            description="Forjado",
            kind="manual",
            total=1000,
        )
        entry.save()
        EntryLine(organization_id=scenario.a.pk, entry=entry, account_id=accounts["1.1"], date=DAY, debit=1000).save()
        EntryLine(organization_id=scenario.a.pk, entry=entry, account_id=accounts["4.1"], date=DAY, credit=900).save()
        entry.apply_seal(actor_id=scenario.member.pk, digest="a" * 64, at=timezone.now())
        with pytest.raises(DatabaseError, match="seal condition failed"), transaction.atomic():
            entry.save(update_fields={"sealed_at", "sealed_by", "seal_digest"})


def test_a_sealed_entry_and_its_lines_never_change(scenario, accounts):
    posted = post(scenario, accounts, "s1")
    with acting_as(scenario.member, scenario.a):
        entry = Entry.objects.get(pk=posted.sysid)
        entry.description = "Outra"
        with pytest.raises(RecordSealed):
            entry.save()
        line = EntryLine.objects.filter(entry=entry).first()
        with pytest.raises(RecordSealed):
            EntryLine(organization_id=scenario.a.pk, entry=entry, account_id=line.account_id, date=DAY, debit=1).save()
        with pytest.raises((RecordSealed, LiveReferencesExist)):
            Entry.objects.get(pk=posted.sysid).delete()
        with connection.cursor() as cursor:
            for statement in (
                "UPDATE skeleton.skeleton_ledger_entry SET description='x', revision=revision+1 WHERE sysid=%s",
                "UPDATE skeleton.skeleton_ledger_entryline SET memo='x', revision=revision+1 WHERE entry_id=%s",
            ):
                with pytest.raises(DatabaseError), transaction.atomic():
                    cursor.execute(statement, [posted.sysid])


def test_the_period_lock_refuses_entries_on_or_before_it(scenario, accounts, book):
    locked = call(scenario, "set_lock", locked_until=DAY, expected_revision=book.revision)
    assert locked.locked_until == DAY
    with pytest.raises(Conflict) as error:
        post(scenario, accounts, "l1")
    assert error.value.code == "period_locked"
    assert post(scenario, accounts, "l2", date=DAY + dt.timedelta(days=1)).number == 1
    with pytest.raises(Conflict) as stale:
        call(scenario, "set_lock", locked_until=None, expected_revision=book.revision)
    assert stale.value.code == "stale_revision"
    reopened = call(scenario, "set_lock", locked_until=None, expected_revision=locked.revision)
    assert reopened.locked_until is None


def test_reversal_swaps_lines_once_and_is_not_reversible(scenario, accounts):
    posted = post(scenario, accounts, "r1")
    reversal = call(scenario, "reverse", entry_id=posted.sysid, date=DAY, reason="erro")
    assert reversal.kind == "reversal" and reversal.reverses_id == posted.sysid and reversal.sealed
    assert sorted((l.debit, l.credit) for l in reversal.lines) == sorted((l.credit, l.debit) for l in posted.lines)
    assert call(scenario, "entry", entry_id=posted.sysid).reversed_by_id == reversal.sysid
    with pytest.raises(Conflict) as error:
        call(scenario, "reverse", entry_id=posted.sysid, date=DAY)
    assert error.value.code == "already_reversed"
    with pytest.raises(InvalidInput):
        call(scenario, "reverse", entry_id=reversal.sysid, date=DAY)


def test_a_concurrent_second_reversal_hits_the_unique_constraint(scenario, accounts, monkeypatch):
    from skeleton.contrib.ledger import posting

    posted = post(scenario, accounts, "c1")
    call(scenario, "reverse", entry_id=posted.sysid, date=DAY)
    real = Entry.objects.filter

    def stale(*args, **kwargs):
        if "reverses" in kwargs:  # The pre-check misses the winner, as in a race.
            return Entry.objects.none()
        return real(*args, **kwargs)

    monkeypatch.setattr(posting.Entry.objects, "filter", stale)
    with pytest.raises(Conflict) as error:
        call(scenario, "reverse", entry_id=posted.sysid, date=DAY)
    assert error.value.code in {"already_reversed", "integrity_conflict"}


def test_reading_managing_and_isolation(scenario, accounts):
    posted = post(scenario, accounts, "i1")
    with pytest.raises(NotFound):
        call(scenario, "entry", user=scenario.denied, entry_id=posted.sysid)
    with pytest.raises(NotFound):
        call(
            scenario,
            "post_manual",
            user=scenario.denied,
            lines=lines(accounts),
            date=DAY,
            description="x",
            idempotency_key="d",
        )
    page = call(scenario, "entries", start=DAY, end=DAY)
    assert [item.sysid for item in page.items] == [posted.sysid]
    with pytest.raises(InvalidInput):
        call(scenario, "entries", start=DAY, end=DAY + dt.timedelta(days=4000))
    call(scenario, "create_default_book", organization=scenario.b)
    call(scenario, "apply_chart", organization=scenario.b)
    assert_tenant_isolated(
        scenario.caller(
            lambda actor: LedgerService().entries(actor=actor, start=DAY, end=DAY), organization=scenario.b
        ),
        foreign=[posted.sysid],
    )
    with pytest.raises(NotFound):
        call(scenario, "reverse", organization=scenario.b, entry_id=posted.sysid, date=DAY)


def test_creation_seal_and_reversal_are_audited_and_a_rollback_leaves_none(scenario, accounts):
    from skeleton.audit.models import AuditEvent

    posted = post(scenario, accounts, "au1")
    call(scenario, "reverse", entry_id=posted.sysid, date=DAY)
    with acting_as(scenario.admin_a, scenario.a):
        operations = list(AuditEvent.objects.filter(record_sysid=posted.sysid).values_list("operation", flat=True))
        before = AuditEvent.objects.filter(table_name="skeleton_ledger_entry").count()
    assert operations.count("create") == 1 and operations.count("update") == 1  # Created, then sealed.
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(RuntimeError), transaction.atomic():
            LedgerService().post_manual(
                actor=actor, lines=lines(accounts), date=DAY, description="x", idempotency_key="au2"
            )
            raise RuntimeError
    with acting_as(scenario.admin_a, scenario.a):
        assert AuditEvent.objects.filter(table_name="skeleton_ledger_entry").count() == before
