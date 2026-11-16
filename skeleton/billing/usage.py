"""Usage meters (#513): consumption that only grows within a billing period, with its terms in prices.

A **meter** is registered in code by the app that consumes it (``register_usage_meter``, re-exported
by ``skeleton.service_api.billing``), with the code ``<app_label>.<name>``. Unlike a state metric of
``quota.py`` (members, records), it counts what was spent in the period: messages sent, documents
signed.

**Terms.** ``Price.usage`` maps a meter to its terms in that price (immutable, like every term):

``{"acme_reminders.messages": {"included": 1000, "overage": "bill", "unit": 100, "unit_amount": 900,
"hard_cap": 20000}}``

- ``included``: allowance per period, multiplied by the item's quantity;
- ``overage``: ``block`` (default) or ``bill``; ``bill`` only in a base plan or app price;
- ``unit`` and ``unit_amount``: required with ``bill`` and forbidden with ``block``; the excess is
  charged in blocks of ``unit`` at ``unit_amount`` (minor unit of the currency);
- ``hard_cap``: optional with ``bill``, the most that may be used in the period (at least
  ``included``); absent means no cap.

A ``usage_pack`` product sells allowance only: its prices may only carry ``included``.

**Resolution** of the terms of a meter ``M`` of app ``a`` in a subscription (``resolve_terms``, pure):
the allowance is the sum over the live items that cite ``M``; the policy is the one of the live
``app`` item of ``a`` that bills ``M``, else of the base plan that bills it, else ``block``; while the
subscription is trialing or set to cancel at period end the policy is ``block`` (there is no next
invoice to charge the excess); a meter no live item cites is unlimited, and its consumption is still
recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

METER = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\Z")
TERM_KEYS = frozenset({"included", "overage", "unit", "unit_amount", "hard_cap"})
MAX_TERM = 10**12


@dataclass(frozen=True, slots=True)
class UsageMeter:
    code: str
    label: Any
    unit_label: Any

    @property
    def app_label(self) -> str:
        return self.code.split(".", 1)[0]


@dataclass(frozen=True, slots=True)
class UsageTerms:
    """The resolved terms of a meter in a subscription; ``overage`` is ``block``, ``bill`` or ``unlimited``."""

    meter: str
    included: int
    overage: str
    unit: int | None = None
    unit_amount: int | None = None
    hard_cap: int | None = None

    @property
    def limit(self) -> int | None:
        """The most that may be used in the period; ``None`` when nothing caps it."""
        if self.overage == "block":
            return self.included
        if self.overage == "bill":
            return self.hard_cap
        return None


_meters: dict[str, UsageMeter] = {}


def register_usage_meter(code: str, *, label: Any, unit_label: Any) -> None:
    """Declare a usage meter from the ``ready()`` of the app; ``code`` is ``<app_label>.<name>``."""
    from .quota import registered_metrics

    if not isinstance(code, str) or not METER.match(code):
        raise ValueError(f"Invalid usage meter code: {code!r} (use <app_label>.<name>).")
    if code in registered_metrics():
        raise ValueError(f"{code} is already a quota metric; a code is either a metric or a meter.")
    meter = UsageMeter(code, label, unit_label)
    existing = _meters.get(code)
    if existing is not None and existing != meter:
        raise ValueError(f"Usage meter {code} is already registered.")
    _meters[code] = meter


def registered_usage_meters() -> tuple[UsageMeter, ...]:
    return tuple(_meters[code] for code in sorted(_meters))


def get_meter(code: str) -> UsageMeter:
    return _meters[code]


def is_meter(code: str) -> bool:
    return code in _meters


def _integer(value: Any, *, low: int) -> bool:
    return type(value) is int and low <= value <= MAX_TERM


def validate_usage(usage: Any, *, product_kind: str | None, check_registered: bool = True) -> None:
    """Refuse invalid ``Price.usage`` terms with an error on the ``usage`` field."""

    def refuse(text: str) -> ValidationError:
        return ValidationError({"usage": text})

    if not isinstance(usage, dict):
        raise refuse(gettext("Use um objeto JSON: medidor → termos."))
    for code, terms in usage.items():
        if not isinstance(code, str) or not METER.match(code):
            raise refuse(gettext("Medidor inválido: %(code)s.") % {"code": code})
        if check_registered and code not in _meters:
            known = ", ".join(sorted(_meters)) or gettext("nenhum")
            raise refuse(
                gettext("Medidor não registrado: %(code)s. Registrados: %(known)s.") % {"code": code, "known": known}
            )
        if not isinstance(terms, dict) or set(terms) - TERM_KEYS:
            raise refuse(
                gettext("Termos de %(code)s: use só included, overage, unit, unit_amount e hard_cap.") % {"code": code}
            )
        included = terms.get("included", 0)
        if not _integer(included, low=0):
            raise refuse(gettext("%(code)s: included é um inteiro não negativo.") % {"code": code})
        overage = terms.get("overage", "block")
        if overage not in ("block", "bill"):
            raise refuse(gettext("%(code)s: overage é block ou bill.") % {"code": code})
        priced = {name for name in ("unit", "unit_amount", "hard_cap") if name in terms}
        if product_kind == "usage_pack" and (overage != "block" or priced or "overage" in terms):
            raise refuse(gettext("Um pacote de uso só traz included."))
        if overage == "block":
            if priced:
                raise refuse(gettext("%(code)s: unit, unit_amount e hard_cap só com overage bill.") % {"code": code})
            continue
        if product_kind not in ("base_plan", "app"):
            raise refuse(gettext("%(code)s: cobrar o excedente só em plano base ou app.") % {"code": code})
        if not (_integer(terms.get("unit"), low=1) and _integer(terms.get("unit_amount"), low=1)):
            raise refuse(gettext("%(code)s: com bill, unit e unit_amount são inteiros positivos.") % {"code": code})
        if "hard_cap" in terms and not (_integer(terms["hard_cap"], low=0) and terms["hard_cap"] >= included):
            raise refuse(gettext("%(code)s: hard_cap é um inteiro maior ou igual a included.") % {"code": code})


def _terms(price: Any, meter: str) -> dict[str, Any] | None:
    usage = getattr(price, "usage", None) or {}
    terms = usage.get(meter)
    return terms if isinstance(terms, dict) else None


def resolve_terms(meter: str, items: Any, *, trialing: bool = False, cancel_scheduled: bool = False) -> UsageTerms:
    """The terms of ``meter`` over the live ``items`` (each with ``price``, ``price.product`` and ``quantity``)."""
    cited = [(item, terms) for item in items if (terms := _terms(item.price, meter)) is not None]
    if not cited:
        return UsageTerms(meter, 0, "unlimited")
    included = sum(int(terms.get("included", 0)) * item.quantity for item, terms in cited)
    app_label = meter.split(".", 1)[0]

    def billing(kind: str) -> dict[str, Any] | None:
        for item, terms in cited:
            product = item.price.product
            if product.kind != kind or terms.get("overage") != "bill":
                continue
            if kind == "app" and product.app_label != app_label:
                continue
            return terms
        return None

    chosen = billing("app") or billing("base_plan")
    if chosen is None or trialing or cancel_scheduled:
        return UsageTerms(meter, included, "block")
    return UsageTerms(meter, included, "bill", chosen["unit"], chosen["unit_amount"], chosen.get("hard_cap"))


def subscription_terms(meter: str, subscription: Any, *, now: Any) -> UsageTerms:
    """``resolve_terms`` over the live items of ``subscription`` at ``now``."""
    from .engine import live_items

    return resolve_terms(
        meter,
        live_items(subscription, now=now),
        trialing=subscription.status == "trialing",
        cancel_scheduled=bool(subscription.cancel_at_period_end),
    )
