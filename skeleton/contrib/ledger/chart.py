"""Charts of accounts declared in code (#543): applied to a book once, idempotently.

Each line is ``(code, name, kind, parent code or None, is_group, base key or None)``.
"""

from __future__ import annotations

from typing import Any

SERVICES = (
    ("1", "Ativo", "asset", None, True, None),
    ("1.1", "Caixa", "asset", "1", False, "ledger.cash"),
    ("1.2", "Bancos", "asset", "1", False, "ledger.bank"),
    ("1.3", "Contas a receber", "asset", "1", False, "ledger.receivables"),
    ("2", "Passivo", "liability", None, True, None),
    ("2.1", "Contas a pagar", "liability", "2", False, "ledger.payables"),
    ("3", "Patrimônio líquido", "equity", None, True, None),
    ("3.1", "Saldo de abertura", "equity", "3", False, "ledger.opening_balance"),
    ("4", "Receitas", "revenue", None, True, None),
    ("4.1", "Receita de serviços", "revenue", "4", False, "ledger.revenue"),
    ("4.2", "Juros recebidos", "revenue", "4", False, "ledger.interest_income"),
    ("4.3", "Descontos obtidos", "revenue", "4", False, "ledger.discounts_received"),
    ("5", "Despesas", "expense", None, True, None),
    ("5.1", "Despesas gerais", "expense", "5", False, "ledger.expenses"),
    ("5.2", "Descontos concedidos", "expense", "5", False, "ledger.discounts_given"),
    ("5.3", "Juros pagos", "expense", "5", False, "ledger.interest_expense"),
    ("5.4", "Tarifas", "expense", "5", False, "ledger.fees"),
)
TEMPLATES: dict[str, Any] = {"services": SERVICES}  # "Prestação de serviços".


def apply_chart(book: Any, template: str = "services") -> int:
    """Create the missing accounts and bindings of ``template`` in ``book``; returns how many were created."""
    from .models import Account, AccountBinding

    created = 0
    accounts = {account.code: account for account in Account.objects.filter(book=book)}
    for code, name, kind, parent, is_group, key in TEMPLATES[template]:
        account = accounts.get(code)
        if account is None:
            account = Account(
                organization_id=book.organization_id,
                book=book,
                code=code,
                name=name,
                kind=kind,
                parent=accounts.get(parent) if parent else None,
                is_group=is_group,
            )
            account.save()
            accounts[code] = account
            created += 1
        if key and not AccountBinding.objects.filter(book=book, key=key).exists():
            AccountBinding(organization_id=book.organization_id, book=book, key=key, account=account).save()
            created += 1
    return created
