"""Balances and reports of a book (#548), computed from the sealed entry lines on every call.

Every number comes from ``EntryLine`` rows whose entry has ``sealed_at`` set: a line without a seal
only exists inside the transaction that creates it and never counts. Balances use the natural sign
of the account kind: debit minus credit for assets and expenses, credit minus debit for liabilities,
equity and revenue. A group account sums its descendants. Each report runs a fixed number of
queries: the accounts of the book and one aggregate (two for the cash flow and the statement).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from django.db.models import BigIntegerField, Count, ExpressionWrapper, F, Q, Sum, Value, Window
from django.db.models.functions import Coalesce, TruncMonth

from .models import Account, AccountBinding, Book, EntryLine, Title

DEBIT_NATURE = frozenset({"asset", "expense"})
AGING_BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def nature(kind: str) -> int:
    """+1 when the account grows with debits, -1 when it grows with credits."""
    return 1 if kind in DEBIT_NATURE else -1


def aging_bucket(due: dt.date, as_of: dt.date) -> str:
    days = (as_of - due).days
    if days <= 0:
        return "current"
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    return "61-90" if days <= 90 else "90+"


def code_key(code: str) -> tuple[Any, ...]:
    """Natural order of account codes: 1.2 before 1.10."""
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in code.split("."))


def sealed_lines(book: Book) -> Any:
    return EntryLine.objects.filter(entry__book=book, entry__sealed_at__isnull=False)


@dataclass
class Node:
    account: Any
    opening_debit: int = 0
    opening_credit: int = 0
    debit: int = 0
    credit: int = 0

    @property
    def sign(self) -> int:
        return nature(self.account.kind)

    @property
    def opening(self) -> int:
        return self.sign * (self.opening_debit - self.opening_credit)

    @property
    def closing(self) -> int:
        return self.opening + self.sign * (self.debit - self.credit)


def chart(book: Book) -> list[Any]:
    """The accounts of the book, in natural code order (one query)."""
    return sorted(Account.objects.filter(book=book), key=lambda account: code_key(account.code))


def descendants(accounts: list[Any]) -> dict[Any, set[Any]]:
    """Each account with itself and every account below it."""
    children: dict[Any, list[Any]] = defaultdict(list)
    for account in accounts:
        if account.parent_id is not None:
            children[account.parent_id].append(account.pk)
    found: dict[Any, set[Any]] = {}

    def walk(pk: Any) -> set[Any]:
        if pk not in found:
            found[pk] = {pk}
            for child in children.get(pk, ()):
                found[pk] |= walk(child)
        return found[pk]

    for account in accounts:
        walk(account.pk)
    return found


def totals(book: Book, *, start: dt.date | None, end: dt.date) -> tuple[list[Any], dict[Any, Node]]:
    """Accounts and, per account (groups summing descendants), the totals before ``start`` and in the window."""
    accounts = chart(book)
    sums: dict[str, Any] = {}
    if start is None:
        sums = {"debit": Coalesce(Sum("debit"), 0), "credit": Coalesce(Sum("credit"), 0)}
    else:
        before, window = Q(date__lt=start), Q(date__gte=start)
        sums = {
            "opening_debit": Coalesce(Sum("debit", filter=before), 0),
            "opening_credit": Coalesce(Sum("credit", filter=before), 0),
            "debit": Coalesce(Sum("debit", filter=window), 0),
            "credit": Coalesce(Sum("credit", filter=window), 0),
        }
    rows = sealed_lines(book).filter(date__lte=end).values("account_id").annotate(**sums)
    own = {row["account_id"]: row for row in rows}
    by_pk = {account.pk: account for account in accounts}
    nodes = {}
    for pk, members in descendants(accounts).items():
        node = Node(by_pk[pk])
        for member in members:
            row = own.get(member)
            if row:
                node.opening_debit += row.get("opening_debit", 0)
                node.opening_credit += row.get("opening_credit", 0)
                node.debit += row["debit"]
                node.credit += row["credit"]
        nodes[pk] = node
    return accounts, nodes


def statement_rows(book: Book, accounts: list[Any], account: Any, *, start: dt.date, end: dt.date) -> tuple[int, Any]:
    """Opening balance of ``account`` (and its descendants) and the ordered lines of the window.

    Each line carries ``running``: the opening balance plus the movement up to it, computed by the
    database over the whole window, so any page of it has the right running balance.
    """
    members = descendants(accounts)[account.pk]
    sign = nature(account.kind)
    lines = sealed_lines(book).filter(account_id__in=members)
    opening = lines.filter(date__lt=start).aggregate(debit=Coalesce(Sum("debit"), 0), credit=Coalesce(Sum("credit"), 0))
    opening_balance = sign * (opening["debit"] - opening["credit"])
    movement = ExpressionWrapper((F("debit") - F("credit")) * Value(sign), output_field=BigIntegerField())
    order = ("date", "entry__year", "entry__number", "sysid")
    rows = (
        lines.filter(date__gte=start, date__lte=end)
        .select_related("entry")
        .annotate(running=Window(Sum(movement), order_by=[F(name).asc() for name in order]))
        .order_by(*order)
    )
    return opening_balance, rows


def cash_accounts(book: Book) -> set[Any]:
    """Accounts bound to ``ledger.cash`` and ``ledger.bank`` (one query)."""
    return set(
        AccountBinding.objects.filter(book=book, key__in=("ledger.cash", "ledger.bank")).values_list(
            "account_id", flat=True
        )
    )


def cash_flow(book: Book, *, start: dt.date, end: dt.date) -> dict[str, Any]:
    """Realized movement of the cash accounts by month, and the open titles falling due by month."""
    accounts = cash_accounts(book)
    lines = sealed_lines(book).filter(account_id__in=accounts, date__lte=end)
    opening = lines.filter(date__lt=start).aggregate(debit=Coalesce(Sum("debit"), 0), credit=Coalesce(Sum("credit"), 0))
    realized = (
        lines.filter(date__gte=start)
        .annotate(month=TruncMonth("date"))
        .values("month")
        .annotate(inflow=Coalesce(Sum("debit"), 0), outflow=Coalesce(Sum("credit"), 0))
    )
    balance = ExpressionWrapper(F("amount") - F("amount_settled"), output_field=BigIntegerField())
    forecast = (
        Title.objects.filter(
            book=book, status__in=(Title.Status.OPEN, Title.Status.PARTIAL), due_date__gte=start, due_date__lte=end
        )
        .annotate(month=TruncMonth("due_date"))
        .values("month")
        .annotate(
            inflow=Coalesce(Sum(balance, filter=Q(direction=Title.Direction.RECEIVABLE)), 0),
            outflow=Coalesce(Sum(balance, filter=Q(direction=Title.Direction.PAYABLE)), 0),
        )
    )
    months: dict[dt.date, dict[str, int]] = defaultdict(
        lambda: {"realized_in": 0, "realized_out": 0, "forecast_in": 0, "forecast_out": 0}
    )
    for row in realized:
        months[_month(row["month"])].update(realized_in=row["inflow"], realized_out=row["outflow"])
    for row in forecast:
        months[_month(row["month"])].update(forecast_in=row["inflow"], forecast_out=row["outflow"])
    return {"opening": opening["debit"] - opening["credit"], "months": dict(sorted(months.items()))}


def _month(value: Any) -> dt.date:
    return value.date() if isinstance(value, dt.datetime) else value


def aging(book: Book, *, as_of: dt.date, direction: str) -> dict[str, tuple[int, int]]:
    """Open balance and count of titles per overdue bucket on ``as_of`` (one query)."""
    balance = ExpressionWrapper(F("amount") - F("amount_settled"), output_field=BigIntegerField())
    days = {
        "current": Q(due_date__gte=as_of),
        "1-30": Q(due_date__lt=as_of, due_date__gte=as_of - dt.timedelta(days=30)),
        "31-60": Q(due_date__lt=as_of - dt.timedelta(days=30), due_date__gte=as_of - dt.timedelta(days=60)),
        "61-90": Q(due_date__lt=as_of - dt.timedelta(days=60), due_date__gte=as_of - dt.timedelta(days=90)),
        "90+": Q(due_date__lt=as_of - dt.timedelta(days=90)),
    }
    aggregates: dict[str, Any] = {}
    for name, condition in days.items():
        aggregates[f"amount_{name}"] = Coalesce(Sum(balance, filter=condition), 0)
        aggregates[f"count_{name}"] = Count("pk", filter=condition)
    row = Title.objects.filter(
        book=book, direction=direction, status__in=(Title.Status.OPEN, Title.Status.PARTIAL), issue_date__lte=as_of
    ).aggregate(**aggregates)
    return {name: (row[f"amount_{name}"], row[f"count_{name}"]) for name in AGING_BUCKETS}
