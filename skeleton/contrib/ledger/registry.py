"""Account keys and posting rules declared in code (#543, #545): never data, never ``eval``.

An app registers them in ``ready()`` through ``skeleton.contrib.ledger.service_api`` (the only
module other apps import). A code starts with the label of the app that registers it
(``<app_label>.<name>``); the ledger's own base keys use ``ledger.``. A duplicate is a ``ValueError``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_CODE = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\Z")
BASE_PREFIX = "ledger"
KINDS = ("asset", "liability", "equity", "revenue", "expense")


@dataclass(frozen=True)
class AccountKey:
    """A role an account plays (cash, revenue of a service): a book binds it to one of its accounts."""

    code: str
    label: Any
    kind: str

    @property
    def app_label(self) -> str:
        prefix = self.code.split(".", 1)[0]
        return "skeleton_ledger" if prefix == BASE_PREFIX else prefix


_keys: dict[str, AccountKey] = {}


def register_account_key(key: AccountKey) -> AccountKey:
    if not isinstance(key, AccountKey) or not _CODE.fullmatch(key.code or ""):
        raise ValueError(f"Invalid account key: {getattr(key, 'code', key)!r}.")
    if key.kind not in KINDS:
        raise ValueError(f"{key.code}: kind must be one of {', '.join(KINDS)}.")
    current = _keys.get(key.code)
    if current is not None and current != key:
        raise ValueError(f"Account key {key.code} is already registered.")
    _keys[key.code] = key
    return key


def get_key(code: str) -> AccountKey:
    return _keys[code]


def all_keys() -> tuple[AccountKey, ...]:
    return tuple(_keys[code] for code in sorted(_keys))


BASE_KEYS = (
    ("cash", "Caixa", "asset"),
    ("bank", "Bancos", "asset"),
    ("receivables", "Contas a receber", "asset"),
    ("payables", "Contas a pagar", "liability"),
    ("revenue", "Receitas", "revenue"),
    ("expenses", "Despesas", "expense"),
    ("discounts_given", "Descontos concedidos", "expense"),
    ("discounts_received", "Descontos obtidos", "revenue"),
    ("interest_income", "Juros recebidos", "revenue"),
    ("interest_expense", "Juros pagos", "expense"),
    ("fees", "Tarifas", "expense"),
    ("opening_balance", "Saldo de abertura", "equity"),
)


def register_base_keys() -> None:
    from django.utils.translation import gettext_lazy

    for name, label, kind in BASE_KEYS:
        register_account_key(AccountKey(code=f"{BASE_PREFIX}.{name}", label=gettext_lazy(label), kind=kind))
