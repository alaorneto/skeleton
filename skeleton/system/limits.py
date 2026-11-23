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
    register_limit_source,
    registered_limit_metrics,
)
from skeleton.tenancy.context import current_context

MEMBERS = "members"
EXTERNAL_PEOPLE = "external_people"
# The instance setting with the default of a metric; a metric not listed has no default.
BASE_SETTINGS = {MEMBERS: "SKELETON_ORGANIZATION_MEMBER_LIMIT"}
# Order of the core sources: the apps (#696) before billing (200) and the platform (#697, 300).
APPS_SOURCE_ORDER = 100
PLATFORM_SOURCE_ORDER = 300
MAX_APP_LIMIT = 100_000


@dataclass(frozen=True)
class LimitStatus:
    metric: str
    used: int
    limit: int | None  # None: unlimited.
    base: int | None
    contributions: tuple[LimitContribution, ...]
    # A fixed limit of the platform (#697) replaced the calculation; contributions are informative.
    fixed: bool = False

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


def _limit(organization_id: UUID, metric: str) -> tuple[int | None, int | None, tuple[LimitContribution, ...], bool]:
    """The limit, the default, the contributions and whether a fixed limit of the platform won."""
    base = base_for(metric)
    contributions = tuple(
        contribution for _, source in limit_sources() for contribution in source(organization_id, metric)
    )
    override = live_override(organization_id, metric)
    if override is not None and override.mode == "fixed":
        return override.value, base, contributions, True
    return combine(base, (item.amount for item in contributions)), base, contributions, False


def limit_status(metric: str, *, organization_id: UUID | None = None) -> LimitStatus:
    """Usage and limit of ``metric`` in the organization of the context, with where the limit comes from."""
    definition = _metric(metric)
    organization_id = _organization(organization_id)
    limit, base, contributions, fixed = _limit(organization_id, metric)
    return LimitStatus(metric, int(definition.counter(organization_id)), limit, base, contributions, fixed)


def check_limit(organization_id: UUID | None, metric: str, increment: int = 1) -> None:
    """Refuse ``increment`` more of ``metric`` above the limit with ``QuotaLimitExceeded``.

    Only in the organization's own context: a call for another organization checks nothing,
    because its rows are not readable here. An increment of zero or less checks nothing.
    """
    definition = _metric(metric)
    if increment <= 0 or organization_id is None or current_context().organization_id != organization_id:
        return
    limit, _, _, _ = _limit(organization_id, metric)
    if limit is None:
        return
    if int(definition.counter(organization_id)) + increment > limit:
        raise QuotaLimitExceeded(metric, limit)


# --------------------------------------------------------------------------- apps (#696)


def validate_app_limits(value: Any) -> dict[str, int]:
    """What an app adds to each limit, as the platform typed it: registered metrics, 1..100000.

    A metric with 0 is dropped (no contribution); the keys come back sorted so an unchanged
    value compares equal and writes nothing.
    """
    from django.core.exceptions import ValidationError
    from django.utils.translation import gettext

    if not isinstance(value, dict):
        raise ValidationError({"limits": gettext("Informe um objeto com o acréscimo de cada métrica.")})
    known = registered_limit_metrics()
    result: dict[str, int] = {}
    for metric, amount in value.items():
        if metric not in known:
            raise ValidationError(
                {"limits": gettext("Métrica de limite desconhecida: %(metric)s.") % {"metric": metric}}
            )
        if type(amount) is not int or not 0 <= amount <= MAX_APP_LIMIT:
            raise ValidationError(
                {
                    "limits": gettext("Use um inteiro de 0 a %(max)s para %(metric)s.")
                    % {"max": MAX_APP_LIMIT, "metric": metric}
                }
            )
        if amount:
            result[metric] = amount
    return dict(sorted(result.items()))


def app_limit_contributions(organization_id: UUID, metric: str) -> tuple[LimitContribution, ...]:
    """The source ``apps``: what each app **effective** in the organization adds to ``metric``.

    Effective is the state the organization actually uses: installed, offered by the platform,
    entitled when billing is on, enabled and with every dependency effective too. It reads the
    organization of the context (activation is tenant data under RLS), so another organization
    contributes nothing here. ``AppAvailability.limits`` is read fresh on every call: a change
    by the platform is valid in the next request of every organization.
    """
    from .applications import application_states
    from .models import AppAvailability

    if current_context().organization_id != organization_id:
        return ()
    effective = sorted(label for label, state in application_states().items() if state["effective"])
    if not effective:
        return ()
    rows = AppAvailability.objects.filter(app_label__in=effective).order_by("app_label")
    return tuple(
        LimitContribution(f"app:{label}", limits[metric])
        for label, limits in rows.values_list("app_label", "limits")
        if isinstance(limits, dict) and type(limits.get(metric)) is int and limits[metric] > 0
    )


# --------------------------------------------------------------------------- platform adjustments (#697)


def live_override(organization_id: UUID, metric: str) -> Any:
    """The adjustment of the platform for (organization, metric) in force now, or ``None``.

    A global row read by organization, never a tenant row: the expiry is checked here, so an
    expired adjustment stops counting without any job.
    """
    from django.db.models import Q
    from django.utils import timezone

    from .models import OrganizationLimitOverride

    return (
        OrganizationLimitOverride.objects.filter(limited_organization_id=organization_id, metric=metric)
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        .first()
    )


def platform_bonus(organization_id: UUID, metric: str) -> tuple[LimitContribution, ...]:
    """The source ``platform``: the bonus in force for the organization (a fixed limit is not a source)."""
    override = live_override(organization_id, metric)
    if override is None or override.mode != "bonus" or not override.value:
        return ()
    return (LimitContribution("platform", override.value),)


MAX_OVERRIDE = 2_147_483_647


def _validated_override(metric: str, mode: str, value: Any, reason: Any, expires_at: Any) -> tuple[int | None, str]:
    from django.core.exceptions import ValidationError
    from django.utils import timezone
    from django.utils.translation import gettext

    if metric not in registered_limit_metrics():
        raise ValidationError({"metric": gettext("Métrica de limite desconhecida: %(metric)s.") % {"metric": metric}})
    if mode not in ("bonus", "fixed"):
        raise ValidationError({"mode": gettext("Use bonus ou fixed.")})
    if value is not None and (type(value) is not int or not 1 <= value <= MAX_OVERRIDE):
        raise ValidationError({"value": gettext("Use um inteiro positivo.")})
    if mode == "bonus" and value is None:
        raise ValidationError({"value": gettext("Informe quanto o bônus soma ao limite.")})
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > 500:
        raise ValidationError({"reason": gettext("Informe o motivo do ajuste (até 500 caracteres).")})
    if expires_at is not None and expires_at <= timezone.now():
        raise ValidationError({"expires_at": gettext("A validade precisa estar no futuro.")})
    return value, reason


def set_limit_override(
    *,
    actor: Any,
    organization: Any,
    metric: str,
    mode: str,
    value: int | None,
    reason: str,
    expires_at: Any = None,
    expected_revision: int = 0,
) -> Any:
    """Give one organization a bonus or a fixed limit of ``metric`` (#697), with CAS.

    ``expected_revision=0`` creates, or restores a removed adjustment (same row, same sysid);
    otherwise it is the revision of the live adjustment the form displayed.
    """
    from django.db import transaction

    from skeleton.accounts.platform import require_platform_context
    from skeleton.common.exceptions import ConcurrentUpdate

    from .models import OrganizationLimitOverride

    require_platform_context(actor)
    value, reason = _validated_override(metric, mode, value, reason, expires_at)
    if type(expected_revision) is not int:
        raise ConcurrentUpdate("Limit adjustment changed.")
    wanted = {"mode": mode, "value": value, "reason": reason, "expires_at": expires_at}
    with transaction.atomic():
        row = (
            OrganizationLimitOverride.all_objects.select_for_update()
            .filter(limited_organization_id=organization.pk, metric=metric)
            .first()
        )
        live = row is not None and row.deleted_at is None
        if (live and row.revision != expected_revision) or (not live and expected_revision != 0):
            raise ConcurrentUpdate("Limit adjustment changed.")
        if row is None:
            row = OrganizationLimitOverride(limited_organization=organization, metric=metric, **wanted)
            row.save()
            return row
        if not live:
            row.restore()
        changed = {name for name, new in wanted.items() if getattr(row, name) != new}
        for name in changed:
            setattr(row, name, wanted[name])
        if changed:
            row.save(update_fields=changed)
        return row


def remove_limit_override(*, actor: Any, organization: Any, metric: str, expected_revision: int) -> None:
    """Remove the adjustment (soft delete): the limit is calculated again."""
    from django.db import transaction

    from skeleton.accounts.platform import require_platform_context
    from skeleton.common.exceptions import ConcurrentUpdate

    from .models import OrganizationLimitOverride

    require_platform_context(actor)
    with transaction.atomic():
        row = (
            OrganizationLimitOverride.objects.select_for_update()
            .filter(limited_organization_id=organization.pk, metric=metric)
            .first()
        )
        if row is None or type(expected_revision) is not int or row.revision != expected_revision:
            raise ConcurrentUpdate("Limit adjustment changed.")
        row.delete()


@dataclass(frozen=True)
class LimitBreakdown:
    """One metric of one organization on the platform page: the status and the adjustment."""

    status: LimitStatus
    label: str
    override: Any  # The live OrganizationLimitOverride row (expired ones included), or None.

    @property
    def expired(self) -> bool:
        from django.utils import timezone

        expires_at = getattr(self.override, "expires_at", None)
        return expires_at is not None and expires_at <= timezone.now()


def limit_breakdown(*, actor: Any, organization: Any) -> tuple[LimitBreakdown, ...]:
    """Every metric of one organization for the platform: base, contributions, adjustment, usage.

    Opens the read-only inspection context of the platform for that organization (like
    ``organization_overview``): counting reads its rows under RLS, and nothing is written.
    """
    from skeleton.access.administration import read_only_inspection
    from skeleton.accounts.platform import require_context_actor, require_platform_admin
    from skeleton.tenancy.context import system_context

    from .models import OrganizationLimitOverride

    require_platform_admin(actor)
    require_context_actor(actor)
    overrides = {
        row.metric: row for row in OrganizationLimitOverride.objects.filter(limited_organization_id=organization.pk)
    }
    with (
        system_context(reason="platform:organization-inspect", organization_id=organization.pk),
        read_only_inspection("default"),
    ):
        return tuple(
            LimitBreakdown(
                limit_status(code, organization_id=organization.pk),
                limit_metric(code).label or code,
                overrides.get(code),
            )
            for code in sorted(registered_limit_metrics())
        )


def register_limits() -> None:
    register_core_metrics()
    register_limit_source("apps", app_limit_contributions, order=APPS_SOURCE_ORDER)
    register_limit_source("platform", platform_bonus, order=PLATFORM_SOURCE_ORDER)
    register_limit_resolver(check_limit)
