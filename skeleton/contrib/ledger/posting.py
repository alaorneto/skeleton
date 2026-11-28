"""Posting core of the ledger (#544): create an entry, add its lines and seal it, in one transaction.

The service checks the lines first (clear errors); the guard proves them again when the seal is set
(``ENTRY_SEAL_CONDITION``). The number is gapless per book and year: the book row is locked while
the next number is taken, so a rollback gives the number back. A correction is never an edit: a
reversal is a new entry with the debits and credits swapped, linked by ``reverses``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.tenancy.context import current_context

from .idempotency import Authorizer, create_or_replay, find_replay
from .models import Account, Book, CostCenter, Entry, EntryLine

SYSTEM_SEALER = uuid.UUID(int=0)  # sealed_by of an entry posted in a system context (a job).
MAX_LINES = 200


@dataclass(frozen=True, slots=True)
class LineDraft:
    account_id: uuid.UUID
    debit: int = 0
    credit: int = 0
    memo: str = ""
    cost_center_id: uuid.UUID | None = None
    party_ref: str = ""
    party_kind: str = ""


class PeriodLocked(Exception):
    """The entry's date is on or before the book's ``locked_until``."""


class AlreadyReversed(Exception):
    """The entry already has a live reversal."""


def _invalid(field: str, message: str) -> ValidationError:
    return ValidationError({field: message})


def check_lines(book: Book, lines: tuple[LineDraft, ...]) -> int:
    """Validate the lines against the book; returns the total (debits = credits)."""
    if not isinstance(lines, tuple) or not 2 <= len(lines) <= MAX_LINES:
        raise _invalid("lines", gettext("Informe de 2 a %(value1)s partidas.") % {"value1": MAX_LINES})
    debit = credit = 0
    for line in lines:
        if not isinstance(line, LineDraft):
            raise _invalid("lines", gettext("Partida inválida."))
        for amount in (line.debit, line.credit):
            if type(amount) is not int or amount < 0:
                raise _invalid("lines", gettext("Valores são inteiros não negativos da menor unidade da moeda."))
        if (line.debit > 0) == (line.credit > 0):
            raise _invalid("lines", gettext("Cada partida tem exatamente um débito ou um crédito."))
        debit += line.debit
        credit += line.credit
    if debit != credit or debit <= 0:
        raise _invalid("lines", gettext("As partidas não fecham: débitos e créditos precisam ser iguais."))
    accounts = {
        row["sysid"]: row
        for row in Account.objects.filter(pk__in={line.account_id for line in lines}).values(
            "sysid", "book_id", "is_group", "is_active"
        )
    }
    for line in lines:
        account = accounts.get(line.account_id)
        if account is None or account["book_id"] != book.pk:
            raise _invalid("lines", gettext("Conta de outro livro ou inexistente."))
        if account["is_group"] or not account["is_active"]:
            raise _invalid("lines", gettext("Use contas analíticas e ativas."))
    centers = {line.cost_center_id for line in lines if line.cost_center_id}
    if centers and CostCenter.objects.filter(pk__in=centers, book=book, is_active=True).count() != len(centers):
        raise _invalid("lines", gettext("Centro de custo de outro livro, inativo ou inexistente."))
    return debit


def _locked_book(book: Book) -> Book:
    return Book.all_objects.select_for_update().get(pk=book.pk)


def _lines_payload(lines: tuple[LineDraft, ...]) -> list[dict[str, Any]]:
    return [
        {
            "account": line.account_id,
            "debit": line.debit,
            "credit": line.credit,
            "memo": line.memo,
            "cost_center": line.cost_center_id,
            "party_ref": line.party_ref,
            "party_kind": line.party_kind,
        }
        for line in lines
    ]


def entry_scope(
    *,
    book_id: Any,
    source_table: str = "",
    source_id: Any = None,
    kind: str,
    reverses_id: Any = None,
) -> dict[str, Any]:
    return {
        "book_id": book_id,
        "source_table": source_table,
        "source_id": source_id,
        "kind": kind,
        "reverses_id": reverses_id,
    }


def create_entry(
    *,
    book: Book,
    lines: tuple[LineDraft, ...],
    date: dt.date,
    description: str,
    kind: str,
    actor_id: Any,
    rule: str = "",
    source_app: str = "",
    source_table: str = "",
    source_id: Any = None,
    idempotency_key: str = "",
    reverses: Entry | None = None,
    authorize_replay: Authorizer | None = None,
) -> Entry:
    """Create, line and seal an entry; an idempotency key already used returns that entry if scope matches."""
    from skeleton.common.sealing import digest
    from skeleton.records.sealing import seal_payload

    if not isinstance(date, dt.date) or isinstance(date, dt.datetime):
        raise _invalid("date", gettext("Use uma data."))
    if not isinstance(description, str) or not description.strip() or len(description) > 300:
        raise _invalid("description", gettext("Descreva o lançamento (até 300 caracteres)."))
    scope = entry_scope(
        book_id=book.pk,
        source_table=source_table,
        source_id=source_id,
        kind=kind,
        reverses_id=reverses.pk if reverses is not None else None,
    )
    found = find_replay(Entry, source_app=source_app, key=idempotency_key, scope=scope, authorize=authorize_replay)
    if found is not None:
        return found
    total = check_lines(book, lines)
    using = current_context().using

    def _insert() -> Entry:
        with transaction.atomic(using=using):
            locked = _locked_book(book)
            if locked.locked_until is not None and date <= locked.locked_until:
                raise PeriodLocked()
            number = (
                Entry.all_objects.filter(book=locked, year=date.year).aggregate(top=Max("number"))["top"] or 0
            ) + 1
            entry = Entry(
                organization_id=locked.organization_id,
                book=locked,
                year=date.year,
                number=number,
                date=date,
                description=description.strip(),
                kind=kind,
                rule=rule,
                source_app=source_app,
                source_table=source_table,
                source_id=source_id,
                idempotency_key=idempotency_key,
                reverses=reverses,
                total=total,
            )
            entry.save()
            for line in lines:
                EntryLine(
                    organization_id=locked.organization_id,
                    entry=entry,
                    account_id=line.account_id,
                    date=date,
                    debit=line.debit,
                    credit=line.credit,
                    memo=line.memo,
                    cost_center_id=line.cost_center_id,
                    party_ref=line.party_ref,
                    party_kind=line.party_kind,
                ).save()
            entry.apply_seal(
                actor_id=actor_id or SYSTEM_SEALER,
                digest=digest(seal_payload(entry, extra={"lines": _lines_payload(lines)})),
                at=timezone.now(),
            )
            entry.save(update_fields={"sealed_at", "sealed_by", "seal_digest"})
        return entry

    def _check_reversal_conflict() -> None:
        if reverses is not None and Entry.objects.filter(reverses=reverses).exists():
            raise AlreadyReversed()

    return create_or_replay(
        Entry,
        source_app=source_app,
        key=idempotency_key,
        scope=scope,
        create=_insert,
        authorize=authorize_replay,
        on_conflict=_check_reversal_conflict,
    )


def entry_lines(entry: Entry) -> tuple[LineDraft, ...]:
    return tuple(
        LineDraft(
            account_id=line.account_id,
            debit=line.debit,
            credit=line.credit,
            memo=line.memo,
            cost_center_id=line.cost_center_id,
            party_ref=line.party_ref,
            party_kind=line.party_kind,
        )
        for line in EntryLine.objects.filter(entry=entry).order_by("sysid")
    )


def reverse_entry(
    *,
    entry: Entry,
    date: dt.date,
    reason: str,
    actor_id: Any,
    idempotency_key: str = "",
    authorize_replay: Authorizer | None = None,
) -> Entry:
    """The inverse entry of ``entry`` (debits and credits swapped), once; a reversal is not reversible."""
    if entry.kind == Entry.Kind.REVERSAL:
        raise _invalid("entry_id", gettext("Um estorno não é estornável."))
    scope = entry_scope(
        book_id=entry.book_id,
        source_table=entry.source_table,
        source_id=entry.source_id,
        kind=Entry.Kind.REVERSAL,
        reverses_id=entry.pk,
    )
    found = find_replay(
        Entry,
        source_app=entry.source_app,
        key=idempotency_key,
        scope=scope,
        authorize=authorize_replay,
    )
    if found is not None:
        return found
    if Entry.objects.filter(reverses=entry).exists():
        raise AlreadyReversed()
    swapped = tuple(
        LineDraft(
            account_id=line.account_id,
            debit=line.credit,
            credit=line.debit,
            memo=line.memo,
            cost_center_id=line.cost_center_id,
            party_ref=line.party_ref,
            party_kind=line.party_kind,
        )
        for line in entry_lines(entry)
    )
    text = (reason or "").strip()
    description = (gettext("Estorno de %(number)s") % {"number": f"{entry.year}/{entry.number}"}) + (
        f": {text}" if text else ""
    )
    return create_entry(
        book=entry.book,
        lines=swapped,
        date=date,
        description=description[:300],
        kind=Entry.Kind.REVERSAL,
        actor_id=actor_id,
        rule=entry.rule,
        source_app=entry.source_app,
        source_table=entry.source_table,
        source_id=entry.source_id,
        idempotency_key=idempotency_key,
        reverses=entry,
        authorize_replay=authorize_replay,
    )


def set_lock(*, book: Book, locked_until: dt.date | None, expected_revision: int) -> Book:
    """Move the period lock of a book, with CAS (audited like any change of the book)."""
    from skeleton.service_api import Conflict

    if locked_until is not None and (not isinstance(locked_until, dt.date) or isinstance(locked_until, dt.datetime)):
        raise _invalid("locked_until", gettext("Use uma data."))
    if type(expected_revision) is not int or book.revision != expected_revision:
        raise Conflict("stale_revision")
    book.locked_until = locked_until
    book.save(update_fields={"locked_until"})
    return book
