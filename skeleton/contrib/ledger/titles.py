"""Receivables, payables and their settlements (#546): the entries they post, by basis.

``amount`` is the principal; a settlement moves ``amount + interest - discount - fee`` of cash for a
receivable and ``amount + interest + fee - discount`` for a payable. Entries:

| Event | Cash basis | Accrual basis |
|---|---|---|
| Create receivable | nothing | D receivables / C category |
| Receive | D payment; D discounts_given; D fees; C category (amount); C interest_income | same, C receivables instead of the category |
| Create payable | nothing | D category / C payables |
| Pay | D category (amount); D interest_expense; D fees; C payment; C discounts_received | same, D payables instead of the category |
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.tenancy.context import current_context

from .models import Account, AccountBinding, Book, Entry, Settlement, Title
from .posting import LineDraft, create_entry, reverse_entry


class AccountNotBound(Exception):
    def __init__(self, key: str):
        super().__init__(key)
        self.key = key


class TitleHasSettlements(Exception):
    pass


class TitleSettled(Exception):
    pass


def bound_account(book: Book, key: str) -> Any:
    account_id = AccountBinding.objects.filter(book=book, key=key).values_list("account_id", flat=True).first()
    if account_id is None:
        raise AccountNotBound(key)
    return account_id


def add_months(day: dt.date, months: int, *, anchor: int | None = None) -> dt.date:
    """``day`` plus ``months``, on ``anchor`` (default the day itself) capped at the month's last day."""
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(anchor or day.day, last))


def _line(account_id: Any, *, debit: int = 0, credit: int = 0, title: Title) -> LineDraft | None:
    if debit == 0 and credit == 0:
        return None
    return LineDraft(
        account_id=account_id,
        debit=debit,
        credit=credit,
        cost_center_id=title.cost_center_id,
        party_ref=title.party_ref,
        party_kind=title.party_kind,
    )


def _source(title: Title) -> dict[str, Any]:
    return {
        "source_app": title.source_app or "skeleton_ledger",
        "source_table": title.source_table,
        "source_id": title.source_id,
    }


def _description(title: Title, prefix: str) -> str:
    return f"{prefix}: {title.description}"[:300]


def create_title(*, book: Book, actor_id: Any, values: dict[str, Any]) -> Title:
    """Create a title; in an accrual book it also posts the recognition entry."""
    category = values["category_account"]
    if category.book_id != book.pk or category.is_group or not category.is_active:
        raise ValidationError(
            {"category_key": gettext("A categoria precisa ser uma conta analítica e ativa do livro.")}
        )
    with transaction.atomic(using=current_context().using):
        title = Title(organization_id=book.organization_id, book=book, **values)
        title.save()
        if book.basis == Book.Basis.ACCRUAL:
            receivable = title.direction == Title.Direction.RECEIVABLE
            counter = bound_account(book, "ledger.receivables" if receivable else "ledger.payables")
            debit, credit = (counter, category.pk) if receivable else (category.pk, counter)
            lines = tuple(
                line
                for line in (
                    _line(debit, debit=title.amount, title=title),
                    _line(credit, credit=title.amount, title=title),
                )
                if line
            )
            entry = create_entry(
                book=book,
                lines=lines,
                date=title.issue_date,
                description=_description(title, gettext("Reconhecimento")),
                kind=Entry.Kind.ACCRUAL,
                actor_id=actor_id,
                idempotency_key=f"title:{title.pk}:accrual",
                **_source(title),
            )
            title.accrual_entry = entry
            title.save(update_fields={"accrual_entry"})
    return title


def settlement_lines(
    title: Title, *, amount: int, discount: int, interest: int, fee: int, payment_account_id: Any
) -> tuple[LineDraft, ...]:
    book = title.book
    accrual = book.basis == Book.Basis.ACCRUAL
    if title.direction == Title.Direction.RECEIVABLE:
        cash = amount + interest - discount - fee
        if cash <= 0:
            raise ValidationError({"amount": gettext("O valor recebido precisa ser positivo.")})
        principal = bound_account(book, "ledger.receivables") if accrual else title.category_account_id
        candidates = (
            _line(payment_account_id, debit=cash, title=title),
            _line(bound_account(book, "ledger.discounts_given"), debit=discount, title=title) if discount else None,
            _line(bound_account(book, "ledger.fees"), debit=fee, title=title) if fee else None,
            _line(principal, credit=amount, title=title),
            _line(bound_account(book, "ledger.interest_income"), credit=interest, title=title) if interest else None,
        )
    else:
        cash = amount + interest + fee - discount
        if cash <= 0:
            raise ValidationError({"amount": gettext("O valor pago precisa ser positivo.")})
        principal = bound_account(book, "ledger.payables") if accrual else title.category_account_id
        candidates = (
            _line(principal, debit=amount, title=title),
            _line(bound_account(book, "ledger.interest_expense"), debit=interest, title=title) if interest else None,
            _line(bound_account(book, "ledger.fees"), debit=fee, title=title) if fee else None,
            _line(payment_account_id, credit=cash, title=title),
            _line(bound_account(book, "ledger.discounts_received"), credit=discount, title=title) if discount else None,
        )
    return tuple(line for line in candidates if line)


def _status(title: Title) -> str:
    if title.amount_settled == 0:
        return Title.Status.OPEN
    return Title.Status.SETTLED if title.amount_settled == title.amount else Title.Status.PARTIAL


def settle(
    *,
    title: Title,
    actor_id: Any,
    amount: int,
    date: dt.date,
    payment_account: Account,
    method: str,
    discount: int,
    interest: int,
    fee: int,
    idempotency_key: str,
) -> Settlement:
    """Settle part or all of the balance; the title row is locked (the caller checked the revision)."""
    for name, value in (("amount", amount), ("discount", discount), ("interest", interest), ("fee", fee)):
        if type(value) is not int or value < 0:
            raise ValidationError({name: gettext("Valores são inteiros não negativos da menor unidade da moeda.")})
    if amount <= 0:
        raise ValidationError({"amount": gettext("Informe o valor baixado.")})
    if title.status == Title.Status.CANCELLED:
        raise ValidationError({"title_id": gettext("O título está cancelado.")})
    if amount > title.balance:
        raise ValidationError({"amount": gettext("A baixa passa do saldo do título.")})
    if payment_account.book_id != title.book_id or payment_account.is_group or not payment_account.is_active:
        raise ValidationError({"payment_account_id": gettext("Use uma conta analítica e ativa do livro.")})
    if method not in Settlement.Method.values:
        raise ValidationError({"method": gettext("Forma de pagamento desconhecida.")})
    lines = settlement_lines(
        title, amount=amount, discount=discount, interest=interest, fee=fee, payment_account_id=payment_account.pk
    )
    with transaction.atomic(using=current_context().using):
        settlement_key = f"title:{title.pk}:settlement:{idempotency_key}" if idempotency_key else ""
        entry = create_entry(
            book=title.book,
            lines=lines,
            date=date,
            description=_description(
                title, gettext("Recebimento") if title.direction == Title.Direction.RECEIVABLE else gettext("Pagamento")
            ),
            kind=Entry.Kind.SETTLEMENT,
            actor_id=actor_id,
            idempotency_key=settlement_key,
            **_source(title),
        )
        settlement = Settlement(
            organization_id=title.organization_id,
            title=title,
            date=date,
            amount=amount,
            discount=discount,
            interest=interest,
            fee=fee,
            payment_account=payment_account,
            method=method,
            idempotency_key=idempotency_key,
            entry=entry,
        )
        settlement.save()
        title.amount_settled += amount
        title.status = _status(title)
        title.save(update_fields={"amount_settled", "status"})
    return settlement


def reverse_settlement(*, settlement: Settlement, title: Title, actor_id: Any, date: dt.date, reason: str) -> Title:
    """Reverse the settlement's entry and give the balance back; the settlement row stays."""
    if settlement.reversal_entry_id is not None:
        raise ValidationError({"settlement_id": gettext("A baixa já foi estornada.")})
    with transaction.atomic(using=current_context().using):
        reversal = reverse_entry(entry=settlement.entry, date=date, reason=reason, actor_id=actor_id)
        settlement.reversal_entry = reversal
        settlement.save(update_fields={"reversal_entry"})
        title.amount_settled -= settlement.amount
        title.status = _status(title)
        title.save(update_fields={"amount_settled", "status"})
    return title


def cancel(*, title: Title, actor_id: Any, date: dt.date, reason: str) -> Title:
    """Cancel a title without live settlements; in an accrual book its recognition is reversed."""
    if title.status == Title.Status.CANCELLED:
        return title
    if Settlement.objects.filter(title=title, reversal_entry__isnull=True).exists():
        raise TitleHasSettlements()
    with transaction.atomic(using=current_context().using):
        if title.accrual_entry is not None:
            reverse_entry(entry=title.accrual_entry, date=date, reason=reason, actor_id=actor_id)
        title.status = Title.Status.CANCELLED
        title.save(update_fields={"status"})
    return title


def split(amount: int, count: int) -> list[int]:
    """Installments that sum to ``amount``, the rounding rest on the last one."""
    base = amount // count
    return [base] * (count - 1) + [amount - base * (count - 1)]
