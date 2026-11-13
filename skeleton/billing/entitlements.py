"""The billing entitlement policy (#396), registered in the core hooks of ``skeleton.common.entitlements``.

What an organization may use comes from its current subscription:

- **Apps:** an app with a product of kind ``app`` is paid. The organization is entitled to it
  while an active item sells it, or while an active base plan includes it. An app without a
  product is free and never closed by billing.
- **Access:** the account's ``access_state``, kept by the dunning job (#399).
- **Storage:** the sum of ``included_storage_bytes × quantity`` of the active items; ``NO_OPINION``
  without a subscription or when no item includes storage, so the platform setting applies.
- **Exempt routes:** the billing pages, for the organization administrator (#404 narrows it to
  the billing capability).

With billing off (``SKELETON_BILLING_ENABLED``) every answer allows everything. The summary is
memoized for the current request and dropped by every billing write (``engine.forget``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from skeleton.common.entitlements import NO_OPINION, AccessLevel
from skeleton.tenancy.context import current_context, request_cache

from . import engine
from .engine import MEMO_KEY, billing_enabled, current_subscription, live_items


@dataclass(frozen=True)
class Summary:
    paid_apps: frozenset[str]
    entitled: frozenset[str]
    access: AccessLevel
    storage: int | None | object


def _summary(organization_id: UUID) -> Summary | None:
    if not billing_enabled():
        return None
    cache = request_cache()
    key = (MEMO_KEY, current_context().using, organization_id)
    if cache is not None and key in cache:
        return cache[key]
    summary = _load(organization_id)
    if cache is not None:
        cache[key] = summary
    return summary


def _load(organization_id: UUID) -> Summary:
    from .engine import account
    from .models import Product

    paid = frozenset(Product.objects.filter(kind=Product.Kind.APP).values_list("app_label", flat=True).distinct())
    if current_context().organization_id != organization_id:
        # The billing rows of another organization are not readable here: fail closed on paid apps.
        return Summary(paid, frozenset(), AccessLevel.ACTIVE, NO_OPINION)
    billing_account = account()
    access = AccessLevel(billing_account.access_state) if billing_account else AccessLevel.ACTIVE
    subscription = current_subscription()
    if subscription is None:
        return Summary(paid, frozenset(), access, NO_OPINION)
    items = live_items(subscription, now=engine.clock())
    entitled: set[str] = set()
    storage = 0
    for item in items:
        if item.app_label:
            entitled.add(item.app_label)
        if item.price.product.kind == Product.Kind.BASE_PLAN:
            entitled.update(item.price.included_apps)
        storage += item.price.included_storage_bytes * item.quantity
    # No item sets an allowance: the instance default applies, never a quota of zero bytes.
    return Summary(paid, frozenset(entitled), access, storage or NO_OPINION)


class BillingPolicy:
    def entitled_apps(self, organization_id: UUID, app_labels: tuple[str, ...]) -> dict[str, bool]:
        summary = _summary(organization_id)
        if summary is None:
            return dict.fromkeys(app_labels, True)
        return {label: label not in summary.paid_apps or label in summary.entitled for label in app_labels}

    def organization_access(self, organization_id: UUID) -> AccessLevel:
        summary = _summary(organization_id)
        return AccessLevel.ACTIVE if summary is None else summary.access

    def storage_quota(self, organization_id: UUID) -> int | None | object:
        summary = _summary(organization_id)
        return NO_OPINION if summary is None else summary.storage

    def may_use_exempt_routes(self, organization_id: UUID, user: Any) -> bool:
        from skeleton.access.policies import is_organization_admin

        if current_context().organization_id != organization_id or user is None or not user.is_authenticated:
            return False
        return is_organization_admin(user)


POLICY = BillingPolicy()
