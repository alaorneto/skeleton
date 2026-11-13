"""Vouchers (#397): a code the platform creates and an organization redeems.

| Kind | ``value`` | Effect |
|---|---|---|
| ``percent`` | basis points (10000 = 100%) | Discount on the eligible lines of each covered renewal |
| ``fixed`` | minor units of ``currency`` | Fixed discount on each covered renewal, never above the eligible lines |
| ``free_cycles`` | ignored | The eligible lines of ``duration_cycles`` renewals cost nothing |
| ``trial_extension`` | days | Extends the current trial once; the subscription must be trialing |

``duration`` says how many renewals a discount covers: ``once`` (one), ``repeating``
(``duration_cycles``) or ``forever``. ``applies_to`` lists product codes; empty means every
product. Discounts apply to renewal invoices, as the last line source, so the total never goes
below zero (the discount is capped by the eligible charges and by the invoice's charges).

Redeeming checks validity, the redemption limit (under the voucher's row lock, so the last
use has one winner), one redemption per organization and the currency of a fixed voucher. A
code that does not exist, is inactive, expired, exhausted or does not fit answers the same
``invalid_voucher``, and attempts are rate limited per organization and per person.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

from . import money
from .engine import LineDraft, _invalid, _now, current_subscription, forget, register_line_source

ATTEMPTS_PER_HOUR = 10
DISCOUNT_ORDER = 900  # After every charge source.


def normalize(code: str) -> str:
    return "".join(str(code or "").split()).upper()


def _refused() -> ValidationError:
    return _invalid("code", gettext("Voucher inválido."), "invalid_voucher")


def _throttle(organization_id: Any, user: Any) -> None:
    from skeleton.common.ratelimit import hit

    keys = [f"billing:voucher:org:{organization_id}"]
    if user is not None and getattr(user, "pk", None):
        keys.append(f"billing:voucher:user:{user.pk}")
    for key in keys:
        if not hit(key, limit=ATTEMPTS_PER_HOUR, window_s=3600).allowed:
            raise _invalid("code", gettext("Muitas tentativas. Tente de novo mais tarde."), "too_many_attempts")


def redeem(code: str, *, user: Any = None, now: datetime | None = None) -> Any:
    """Redeem ``code`` for the current organization's subscription; returns the redemption."""
    from .models import Subscription, Voucher, VoucherRedemption

    now = _now(now)
    subscription = current_subscription(lock=True)
    if subscription is None:
        raise _invalid("code", gettext("Assine um plano antes de usar um voucher."), "no_subscription")
    _throttle(subscription.organization_id, user)
    voucher = Voucher.objects.select_for_update().filter(code=normalize(code)).first()
    if voucher is not None and VoucherRedemption.objects.filter(voucher=voucher).exists():
        raise _invalid("code", gettext("Este voucher já foi usado nesta organização."), "already_redeemed")
    if voucher is None or not voucher.usable(now=now):
        raise _refused()
    if voucher.kind == Voucher.Kind.FIXED and voucher.currency != subscription.currency:
        raise _refused()
    if voucher.kind == Voucher.Kind.TRIAL_EXTENSION and subscription.status != Subscription.Status.TRIALING:
        raise _invalid("code", gettext("Este voucher só vale durante o período de avaliação."), "not_trialing")
    voucher.redemption_count += 1
    voucher.save(update_fields={"redemption_count"})
    redemption = VoucherRedemption(
        organization_id=subscription.organization_id,
        voucher=voucher,
        subscription=subscription,
        redeemed_at=now,
        remaining_cycles=voucher.covered_cycles(),
    )
    if voucher.kind == Voucher.Kind.TRIAL_EXTENSION:
        _extend_trial(subscription, days=voucher.value)
        redemption.remaining_cycles = 0
    redemption.save()
    forget()
    return redemption


def _extend_trial(subscription: Any, *, days: int) -> None:
    extra = timedelta(days=days)
    old_end = subscription.trial_end
    subscription.trial_end = old_end + extra
    subscription.current_period_end = subscription.current_period_end + extra
    subscription.billing_anchor_day = subscription.current_period_end.day
    subscription.save(update_fields={"trial_end", "current_period_end", "billing_anchor_day"})
    for item in subscription.items.filter(trial_end=old_end):
        item.trial_end = subscription.trial_end
        item.save(update_fields={"trial_end"})


def _eligible(lines: list[LineDraft], applies_to: list[str]) -> int:
    total = 0
    for line in lines:
        if line.kind == "discount" or line.amount <= 0:
            continue
        product = getattr(getattr(line, "price", None), "product", None)
        if applies_to and (product is None or product.code not in applies_to):
            continue
        total += line.amount
    return total


def discount_lines(
    subscription: Any, period_start: datetime, period_end: datetime, lines: list[LineDraft], now: datetime
) -> list[LineDraft]:
    """The discounts of the active redemptions on a renewal invoice (registered as a line source)."""
    from .models import Voucher, VoucherRedemption

    charges = sum(line.amount for line in lines if line.kind != "discount")
    result: list[LineDraft] = []
    redemptions = (
        VoucherRedemption.objects.select_for_update()
        .filter(subscription=subscription)
        .exclude(remaining_cycles=0)
        .select_related("voucher")
        .order_by("redeemed_at", "sysid")
    )
    for redemption in redemptions:
        voucher = redemption.voucher
        eligible = _eligible(lines, voucher.applies_to)
        if voucher.kind == Voucher.Kind.PERCENT:
            amount = money.percent(eligible, voucher.value)
        elif voucher.kind == Voucher.Kind.FIXED:
            amount = min(voucher.value, eligible)
        elif voucher.kind == Voucher.Kind.FREE_CYCLES:
            amount = eligible
        else:
            continue
        given = -sum(line.amount for line in result)
        amount = max(0, min(amount, charges - given))
        if amount:
            result.append(
                LineDraft(
                    kind="discount",
                    description=gettext("Voucher %(code)s") % {"code": voucher.code},
                    amount=-amount,
                    unit_amount=-amount,
                )
            )
        if redemption.remaining_cycles is not None:
            redemption.remaining_cycles -= 1
            redemption.save(update_fields={"remaining_cycles"})
    return result


def register_voucher_lines() -> None:
    register_line_source(discount_lines, order=DISCOUNT_ORDER)
