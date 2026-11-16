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


# --------------------------------------------------------------------------- posting rules (#545)

SIDES = ("debit", "credit")
ACTIONS = ("read", "update", "create", "delete")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,49}\Z")


@dataclass(frozen=True)
class Key:
    """An account key resolved through the book's binding."""

    code: str


@dataclass(frozen=True)
class Param:
    """An account the caller passes (``accounts={name: account_id}``), such as which cash or bank."""

    name: str


@dataclass(frozen=True)
class RuleLine:
    side: str
    account: Key | Param
    amount: str  # Name of the component in ``amounts``.


@dataclass(frozen=True)
class PostingRule:
    """How a record of an app becomes an entry: declared in code, authorized by the source record."""

    code: str
    label: Any
    source_table_key: str
    source_action: str
    lines: tuple[RuleLine, ...]

    @property
    def app_label(self) -> str:
        return self.code.split(".", 1)[0]


_rules: dict[str, PostingRule] = {}


def register_posting_rule(rule: PostingRule) -> PostingRule:
    """Register a rule; the source table is checked at the end of app loading (check E041)."""
    if not isinstance(rule, PostingRule) or not _CODE.fullmatch(rule.code or ""):
        raise ValueError(f"Invalid posting rule: {getattr(rule, 'code', rule)!r}.")
    if not isinstance(rule.source_table_key, str) or not _CODE.fullmatch(rule.source_table_key):
        raise ValueError(f"{rule.code}: source_table_key must be <app_label>.<model>.")
    if rule.source_table_key.split(".", 1)[0] != rule.app_label:
        raise ValueError(f"{rule.code}: the source table belongs to the app that registers the rule.")
    if rule.source_action not in ACTIONS:
        raise ValueError(f"{rule.code}: source_action must be one of {', '.join(ACTIONS)}.")
    if not isinstance(rule.lines, tuple) or len(rule.lines) < 2:
        raise ValueError(f"{rule.code}: a rule has at least two lines.")
    for line in rule.lines:
        if not isinstance(line, RuleLine) or line.side not in SIDES:
            raise ValueError(f"{rule.code}: each line is a RuleLine with side debit or credit.")
        if not isinstance(line.amount, str) or not _NAME.fullmatch(line.amount):
            raise ValueError(f"{rule.code}: each line names its amount component.")
        if isinstance(line.account, Key):
            if not _CODE.fullmatch(line.account.code or ""):
                raise ValueError(f"{rule.code}: invalid account key {line.account.code!r}.")
        elif not isinstance(line.account, Param) or not _NAME.fullmatch(line.account.name or ""):
            raise ValueError(f"{rule.code}: each line takes a Key or a Param.")
    current = _rules.get(rule.code)
    if current is not None and current != rule:
        raise ValueError(f"Posting rule {rule.code} is already registered.")
    _rules[rule.code] = rule
    return rule


def get_rule(code: str) -> PostingRule:
    return _rules[code]


def all_rules() -> tuple[PostingRule, ...]:
    return tuple(_rules[code] for code in sorted(_rules))
