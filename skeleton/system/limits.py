"""The core resolver of organization limits (#695), with or without billing.

``skeleton.common.limits`` holds the registry and the rule; this module adds what needs the ORM:
the core metrics, the instance default of each metric and the resolver that counts and refuses.
It is registered in ``SystemConfig.ready``. Billing registers its plan as one more source
(``skeleton/billing/quota.py``), so the subscription adds to the limit instead of owning it.

- ``members`` counts active memberships of **people**: service accounts are integrations and
  external people of the portal have their own metric (``external_people``).
- The instance default of ``members`` is ``SKELETON_ORGANIZATION_MEMBER_LIMIT`` (empty or 0: no
  default). The other metrics have no default.
- Counting happens in the organization's own context (RLS), under the ``organization_lock`` the
  membership services already hold, so two admissions for the last seat have one winner.
- A lower limit never removes anyone: the organization is over the limit and admits nobody
  until it is back under it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.conf import settings

from skeleton.common.exceptions import MissingTenantContext, QuotaLimitExceeded
from skeleton.common.limits import (
    LimitContribution,
    combine,
    limit_metric,
    limit_sources,
    register_limit_metric,
    register_limit_resolver,
)
from skeleton.tenancy.context import current_context

MEMBERS = "members"
EXTERNAL_PEOPLE = "external_people"
# The instance setting with the default of a metric; a metric not listed has no default.
BASE_SETTINGS = {MEMBERS: "SKELETON_ORGANIZATION_MEMBER_LIMIT"}


@dataclass(frozen=True)
class LimitStatus:
    metric: str
    used: int
    limit: int | None  # None: unlimited.
    base: int | None
    contributions: tuple[LimitContribution, ...]

    @property
    def over_limit(self) -> bool:
        return self.limit is not None and self.used > self.limit

    @property
    def remaining(self) -> int | None:
        return None if self.limit is None else max(self.limit - self.used, 0)


def count_members(organization_id: Any) -> int:
    """Active memberships of people in the organization (never service accounts)."""
    from skeleton.accounts.models import AccountType
    from skeleton.tenancy.models import Membership

    return Membership.objects.filter(
        organization_id=organization_id, is_active=True, user__account_type=AccountType.HUMAN
    ).count()


def count_external_people(organization_id: Any) -> int:
    """Active external accesses of the portal (#531); never counted in ``members``."""
    from skeleton.tenancy.models import ExternalAccess

    return ExternalAccess.objects.filter(organization_id=organization_id, is_active=True).count()


def register_core_metrics() -> None:
    register_limit_metric(MEMBERS, count_members, label="Membros")
    register_limit_metric(EXTERNAL_PEOPLE, count_external_people, label="Pessoas externas")


def base_for(metric: str) -> int | None:
    """The instance default of ``metric``; ``None`` when the instance sets none."""
    name = BASE_SETTINGS.get(metric)
    value = getattr(settings, name, None) if name else None
    if value is None or isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _metric(code: str) -> Any:
    try:
        return limit_metric(code)
    except KeyError:
        raise ValueError(f"Unknown limit metric {code!r}.") from None


def _organization(organization_id: UUID | None) -> UUID:
    organization_id = organization_id or current_context().organization_id
    if organization_id is None:
        raise MissingTenantContext("Limits are decided per organization.")
    return organization_id


def _limit(organization_id: UUID, metric: str) -> tuple[int | None, int | None, tuple[LimitContribution, ...]]:
    base = base_for(metric)
    contributions = tuple(
        contribution for _, source in limit_sources() for contribution in source(organization_id, metric)
    )
    return combine(base, (item.amount for item in contributions)), base, contributions


def limit_status(metric: str, *, organization_id: UUID | None = None) -> LimitStatus:
    """Usage and limit of ``metric`` in the organization of the context, with where the limit comes from."""
    definition = _metric(metric)
    organization_id = _organization(organization_id)
    limit, base, contributions = _limit(organization_id, metric)
    return LimitStatus(metric, int(definition.counter(organization_id)), limit, base, contributions)


def check_limit(organization_id: UUID | None, metric: str, increment: int = 1) -> None:
    """Refuse ``increment`` more of ``metric`` above the limit with ``QuotaLimitExceeded``.

    Only in the organization's own context: a call for another organization checks nothing,
    because its rows are not readable here. An increment of zero or less checks nothing.
    """
    definition = _metric(metric)
    if increment <= 0 or organization_id is None or current_context().organization_id != organization_id:
        return
    limit, _, _ = _limit(organization_id, metric)
    if limit is None:
        return
    if int(definition.counter(organization_id)) + increment > limit:
        raise QuotaLimitExceeded(metric, limit)


def register_limits() -> None:
    register_core_metrics()
    register_limit_resolver(check_limit)
