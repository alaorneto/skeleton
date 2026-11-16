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


# --------------------------------------------------------------------------- consumption (#523)

KEY = re.compile(r"[A-Za-z0-9:._-]{1,200}\Z")
MAX_QUANTITY = 1_000_000


class UsageVoided(Exception):
    """The idempotency key belongs to a consumption that was given back."""


class UsagePeriodClosed(Exception):
    """The consumption belongs to a period that already closed."""


class UsageNotFound(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Receipt:
    meter: str
    metered: bool
    created: bool
    used: int
    included: int
    limit: int | None
    overage: str


@dataclass(frozen=True, slots=True)
class Status:
    meter: str
    used: int
    included: int
    limit: int | None
    overage: str
    period_start: Any
    period_end: Any


def _validate(meter: Any, *, quantity: Any = 1, key: Any = None) -> None:
    errors: dict[str, str] = {}
    if not isinstance(meter, str) or meter not in _meters:
        errors["meter"] = gettext("Medidor não registrado.")
    if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
        errors["quantity"] = gettext("Use um inteiro de 1 a 1.000.000.")
    if key is not None and (not isinstance(key, str) or not KEY.match(key)):
        errors["idempotency_key"] = gettext("Use até 200 letras, dígitos, dois-pontos, ponto, sublinhado ou hífen.")
    if errors:
        raise ValidationError(errors)


def attribution_start(subscription: Any, now: Any) -> Any:
    """The period a consumption at ``now`` belongs to: the next one once the current period ended."""
    if now < subscription.current_period_end:
        return subscription.current_period_start
    return subscription.current_period_end


def _months(subscription: Any) -> int:
    return subscription.interval_count * (12 if subscription.interval == "year" else 1)


def _period_end(subscription: Any, start: Any) -> Any:
    from .periods import add_months

    if start == subscription.current_period_start:
        return subscription.current_period_end
    return add_months(start, _months(subscription))


def _balance(meter: str, start: Any, *, lock: bool) -> Any:
    """The counter row of ``meter`` in the period; with ``lock``, created when missing and locked."""
    from django.db import IntegrityError, transaction

    from skeleton.tenancy.context import current_context

    from .engine import _organization_id
    from .models import UsageBalance

    queryset = UsageBalance.objects.filter(meter=meter, period_start=start)
    if not lock:
        return queryset.first()
    row = queryset.select_for_update().first()
    if row is not None:
        return row
    try:
        with transaction.atomic(using=current_context().using):
            UsageBalance(organization_id=_organization_id(), meter=meter, period_start=start).save()
    except IntegrityError:
        pass  # Another transaction created it first: lock theirs.
    return queryset.select_for_update().get()


def _receipt(meter: str, terms: UsageTerms, used: int, *, created: bool) -> Receipt:
    return Receipt(meter, True, created, used, terms.included, terms.limit, terms.overage)


def _unmetered(meter: str) -> Receipt:
    return Receipt(meter, False, False, 0, 0, None, "unlimited")


def record(meter: str, *, quantity: int = 1, idempotency_key: str, now: Any = None) -> Receipt:
    """Count ``quantity`` of ``meter`` in the current organization, in the caller's transaction.

    Billing off or no subscription: nothing is recorded (``metered=False``). A repeated key returns
    the receipt of the first call (``created=False``); a key that was voided raises ``UsageVoided``.
    Above the limit (the allowance with ``block``, the hard cap with ``bill``) it raises
    ``QuotaLimitExceeded`` and records nothing.
    """
    from django.db import IntegrityError, transaction

    from skeleton.common.exceptions import QuotaLimitExceeded
    from skeleton.tenancy.context import current_context

    from . import engine
    from .models import UsageEvent

    _validate(meter, quantity=quantity, key=idempotency_key)
    if not engine.billing_enabled():
        return _unmetered(meter)
    subscription = engine.current_subscription()
    if subscription is None:
        return _unmetered(meter)
    now = engine._now(now)
    start = attribution_start(subscription, now)
    terms = subscription_terms(meter, subscription, now=now)
    existing = UsageEvent.objects.filter(meter=meter, idempotency_key=idempotency_key).first()
    if existing is not None:
        return _repeat(meter, existing, terms)
    try:
        with transaction.atomic(using=current_context().using):
            UsageEvent(
                organization_id=subscription.organization_id,
                meter=meter,
                quantity=quantity,
                occurred_at=now,
                period_start=start,
                idempotency_key=idempotency_key,
                source_app=meter.split(".", 1)[0],
            ).save()
            balance = _balance(meter, start, lock=True)
            limit = terms.limit
            if limit is not None and balance.used + quantity > limit:
                raise QuotaLimitExceeded(meter, limit)
            balance.used += quantity
            balance.save(update_fields={"used", "updated_at"})
    except IntegrityError:
        # The same key won a race: answer like the repeated call.
        return _repeat(meter, UsageEvent.objects.get(meter=meter, idempotency_key=idempotency_key), terms)
    return _receipt(meter, terms, balance.used, created=True)


def _repeat(meter: str, event: Any, terms: UsageTerms) -> Receipt:
    if event.voided_at is not None:
        raise UsageVoided(meter)
    balance = _balance(meter, event.period_start, lock=False)
    return _receipt(meter, terms, balance.used if balance else 0, created=False)


def void(meter: str, *, idempotency_key: str, now: Any = None) -> Receipt:
    """Give back a consumption of the open period; voiding twice returns the same receipt."""
    from . import engine
    from .models import UsageEvent

    _validate(meter, key=idempotency_key)
    subscription = engine.current_subscription() if engine.billing_enabled() else None
    if subscription is None:
        raise UsageNotFound(meter)
    event = UsageEvent.objects.select_for_update().filter(meter=meter, idempotency_key=idempotency_key).first()
    if event is None:
        raise UsageNotFound(meter)
    now = engine._now(now)
    terms = subscription_terms(meter, subscription, now=now)
    balance = _balance(meter, event.period_start, lock=True)
    if event.voided_at is None:
        if event.period_start < subscription.current_period_start:
            raise UsagePeriodClosed(meter)
        event.voided_at = now
        event.save(update_fields={"voided_at", "updated_at"})
        balance.used = max(balance.used - event.quantity, 0)
        balance.save(update_fields={"used", "updated_at"})
    return _receipt(meter, terms, balance.used, created=False)


def status(meter: str, *, now: Any = None) -> Status:
    """Used, allowance and limit of ``meter`` in the period a consumption at ``now`` goes to."""
    from . import engine

    _validate(meter)
    subscription = engine.current_subscription() if engine.billing_enabled() else None
    if subscription is None:
        return Status(meter, 0, 0, None, "unlimited", None, None)
    now = engine._now(now)
    start = attribution_start(subscription, now)
    terms = subscription_terms(meter, subscription, now=now)
    balance = _balance(meter, start, lock=False)
    used = balance.used if balance else 0
    return Status(meter, used, terms.included, terms.limit, terms.overage, start, _period_end(subscription, start))


def check(meter: str, *, quantity: int = 1, now: Any = None) -> Status:
    """``status``, raising ``QuotaLimitExceeded`` when ``quantity`` more would not fit; records nothing."""
    from skeleton.common.exceptions import QuotaLimitExceeded

    _validate(meter, quantity=quantity)
    found = status(meter, now=now)
    if found.limit is not None and found.used + quantity > found.limit:
        raise QuotaLimitExceeded(meter, found.limit)
    return found


def purge_usage(*, now: Any = None) -> int:
    """Delete events and counters of periods older than the retention, never the current or previous one."""
    from datetime import timedelta

    from django.conf import settings

    from . import engine
    from .models import UsageBalance, UsageEvent
    from .periods import add_months

    now = engine._now(now)
    days = int(getattr(settings, "SKELETON_BILLING_USAGE_RETENTION_DAYS", 400))
    cutoff = now - timedelta(days=days)
    subscription = engine.current_subscription()
    if subscription is not None:
        cutoff = min(cutoff, add_months(subscription.current_period_start, -_months(subscription)))
    events, _ = UsageEvent.objects.filter(period_start__lt=cutoff).delete()
    balances, _ = UsageBalance.objects.filter(period_start__lt=cutoff).delete()
    return events + balances


# --------------------------------------------------------------------------- invoicing (#524)

USAGE_ORDER = 510  # After the storage overage (500), before the discounts.


def usage_lines(subscription: Any, period_start: Any, period_end: Any, lines: list[Any], now: Any) -> list[Any]:
    """The billed excess of each meter in the closing period, on its renewal invoice (a line source).

    The terms are those of the items live in the last instant of the period, so a pack contracted
    in the middle of it counts; a closing period in trial, or with ``block``, gives no line. One line
    per meter, by code: ``ceil((used - included) / unit)`` blocks at ``unit_amount``.
    """
    import math
    from datetime import timedelta

    from .engine import LineDraft, live_items
    from .models import UsageBalance

    balances = list(UsageBalance.objects.filter(period_start=period_start, used__gt=0).order_by("meter"))
    if not balances:
        return []
    items = live_items(subscription, now=period_end - timedelta(microseconds=1), statuses=("active", "ended"))
    trial = subscription.trial_end is not None and subscription.trial_end >= period_end
    found = []
    for balance in balances:
        terms = resolve_terms(balance.meter, items, trialing=trial)
        if terms.overage != "bill" or balance.used <= terms.included or not terms.unit or not terms.unit_amount:
            continue
        blocks = math.ceil((balance.used - terms.included) / terms.unit)
        meter = _meters.get(balance.meter)
        label = str(meter.label) if meter else balance.meter
        found.append(
            LineDraft(
                kind="overage",
                description=gettext("%(label)s excedente (%(meter)s)") % {"label": label, "meter": balance.meter},
                amount=blocks * terms.unit_amount,
                unit_amount=terms.unit_amount,
                quantity=blocks,
                app_label=balance.meter.split(".", 1)[0],
                period_start=period_start,
                period_end=period_end,
                metric=balance.meter,
            )
        )
    return found


def register_usage() -> None:
    from .engine import register_line_source

    register_line_source(usage_lines, order=USAGE_ORDER)
