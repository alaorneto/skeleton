"""Organization limits (#695): the registry of metrics and sources, and the combination rule.

A **metric** is something an organization has a limited amount of, counted at the moment of the
check: ``members``, ``external_people`` or an app's ``<app_label>.<name>``. A **source** says how
much it adds to the limit of a metric in one organization (the subscription's plan, the apps the
organization uses, an adjustment of the platform). The limit is::

    base = the instance default for the metric (None: no default)
    if base is None and no source contributes: unlimited
    otherwise: limit = (base or 0) + sum(contributions)

so an instance without a default and without contributions stays unlimited, as before #695.

The resolver that counts and refuses is registered by ``skeleton.system`` in ``ready()``; the
domain packages only call ``check`` (through ``skeleton.common.entitlements.check_limit``).
Without a resolver nothing is limited. Pure Python, no ``skeleton.*`` import (rule R1).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

METRIC_CODE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?\Z")


@dataclass(frozen=True)
class LimitMetric:
    code: str
    counter: Callable[[Any], int]  # Current amount, called in the organization's own context.
    label: str = ""


@dataclass(frozen=True)
class LimitContribution:
    """What one source adds to the limit of a metric: ``source`` names it (``billing``, ``app:<label>``)."""

    source: str
    amount: int


# A source answers the contributions it has for (organization, metric); empty: no opinion.
LimitSource = Callable[[UUID, str], Iterable[LimitContribution]]
# The resolver of the core: refuses ``increment`` more of the metric above the limit.
LimitResolver = Callable[[UUID, str, int], None]

_metrics: dict[str, LimitMetric] = {}
_sources: dict[str, tuple[int, LimitSource]] = {}
_resolver: LimitResolver | None = None


def register_limit_metric(code: str, counter: Callable[[Any], int], *, label: str = "") -> None:
    """Declare a metric; ``code`` is ``members``-like or ``<app_label>.<name>``. Same counter again: no-op."""
    if not isinstance(code, str) or not METRIC_CODE.match(code):
        raise ValueError(f"Invalid limit metric code: {code!r}.")
    existing = _metrics.get(code)
    if existing is not None:
        if existing.counter is not counter:
            raise ValueError(f"Limit metric {code} is already registered.")
        return
    _metrics[code] = LimitMetric(code, counter, label)


def registered_limit_metrics() -> frozenset[str]:
    return frozenset(_metrics)


def limit_metric(code: str) -> LimitMetric:
    """The registered metric; ``KeyError`` for an unknown code."""
    return _metrics[code]


def register_limit_source(name: str, source: LimitSource, *, order: int) -> None:
    """Add a source; sources run by ``order``. The same source again under its name is a no-op."""
    existing = _sources.get(name)
    if existing is not None and existing != (order, source):
        raise ValueError(f"Limit source {name} is already registered.")
    _sources[name] = (order, source)


def limit_sources() -> tuple[tuple[str, LimitSource], ...]:
    return tuple((name, source) for name, (_, source) in sorted(_sources.items(), key=lambda item: item[1][0]))


def combine(base: int | None, contributions: Iterable[int]) -> int | None:
    """The limit from the instance default and the contributions; ``None`` is unlimited."""
    amounts = list(contributions)
    if base is None and not amounts:
        return None
    return (base or 0) + sum(amounts)


def register_limit_resolver(resolver: LimitResolver) -> None:
    """Register the single resolver (``skeleton.system`` in ``ready()``); a different second one is refused."""
    global _resolver
    if _resolver is not None and _resolver is not resolver:
        raise ValueError("A limit resolver is already registered.")
    _resolver = resolver


def check(organization_id: UUID, metric: str, increment: int = 1) -> None:
    """Refuse ``increment`` more of ``metric`` above the limit (``QuotaLimitExceeded``); no resolver, no limit."""
    if _resolver is not None:
        _resolver(organization_id, metric, increment)
