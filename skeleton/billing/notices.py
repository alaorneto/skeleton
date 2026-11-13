"""Billing notifications (#399): mandatory, to the organization's administrators, once per step.

Each type has no target table (billing tables have no record-level authorization): the
fan-out re-checks that each person still administers the organization (``authorize``), and
``open_url`` leads to the billing area. ``dedupe_key`` makes every step happen once, even when
the job runs again.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.utils.translation import gettext_lazy

FIELDS = ("number", "total", "currency", "due_at", "state", "trial_end")
TYPES = {
    "billing.trial_ending": gettext_lazy("Período de avaliação terminando"),
    "billing.invoice_issued": gettext_lazy("Nova fatura"),
    "billing.invoice_overdue": gettext_lazy("Fatura em atraso"),
    "billing.payment_failed": gettext_lazy("Pagamento recusado"),
    "billing.payment_received": gettext_lazy("Pagamento confirmado"),
    "billing.access_restricted": gettext_lazy("Organização em modo somente leitura"),
    "billing.access_suspended": gettext_lazy("Organização suspensa"),
}


def _authorize(user: Any, notification: Any) -> bool:
    from skeleton.access.capabilities import BILLING_VIEW, has_capability

    return has_capability(user, BILLING_VIEW)


def _open_url(user: Any, row: Any) -> str | None:
    from django.urls import NoReverseMatch, reverse

    from skeleton.tenancy.context import current_context

    organization_id = current_context().organization_id
    for name in ("billing-overview", "web:dashboard"):
        try:
            return reverse(name, kwargs={"organization_id": organization_id})
        except NoReverseMatch:
            continue
    return None


def register_notices() -> None:
    from skeleton.notifications.registry import NotificationType, register_notification_type

    from .engine import register_after_issue, register_after_payment

    register_after_issue(invoice_issued)
    register_after_payment(payment_received)

    for code, label in TYPES.items():
        register_notification_type(
            NotificationType(
                code,
                str(label),
                payload_fields=FIELDS,
                default_channels=("in_app", "email"),
                mandatory=True,
                owner_app="sk_billing",
                open_url=_open_url,
                authorize=_authorize,
            )
        )


def _notify(code: str, *, dedupe_key: str, **payload: Any) -> None:
    from skeleton.access.models import Role
    from skeleton.notifications.services import Recipients, notify

    admins = Role.objects.filter(code="organization_admin").values_list("pk", flat=True).first()
    if admins is None:
        return
    notify(code, recipients=Recipients(roles=(admins,)), payload=payload, dedupe_key=dedupe_key[:200])


def _invoice(invoice: Any) -> dict[str, Any]:
    return {
        "number": invoice.number,
        "total": invoice.total,
        "currency": invoice.currency,
        "due_at": invoice.due_at.isoformat(),
    }


def invoice_issued(invoice: Any) -> None:
    if invoice.total > 0:
        _notify("billing.invoice_issued", dedupe_key=f"invoice:{invoice.pk}:issued", **_invoice(invoice))


def invoice_overdue(invoice: Any) -> None:
    _notify("billing.invoice_overdue", dedupe_key=f"invoice:{invoice.pk}:overdue", **_invoice(invoice))


def payment_failed(invoice: Any, *, attempt: int) -> None:
    _notify("billing.payment_failed", dedupe_key=f"invoice:{invoice.pk}:failed:{attempt}", **_invoice(invoice))


def payment_received(invoice: Any) -> None:
    if invoice.total > 0:
        _notify("billing.payment_received", dedupe_key=f"invoice:{invoice.pk}:paid", **_invoice(invoice))


def access_changed(billing_account: Any, *, previous: str, now: datetime) -> None:
    state = billing_account.access_state
    if state not in ("restricted", "suspended"):
        return
    _notify(
        f"billing.access_{state}",
        dedupe_key=f"account:{billing_account.pk}:{state}:{now.date().isoformat()}",
        state=state,
    )


def trial_ending(*, now: datetime, days: int) -> None:
    from . import engine

    subscription = engine.current_subscription()
    if subscription is None or subscription.status != "trialing" or subscription.trial_end is None:
        return
    if now < subscription.trial_end <= now + timedelta(days=days):
        _notify(
            "billing.trial_ending",
            dedupe_key=f"subscription:{subscription.pk}:trial:{subscription.trial_end.date().isoformat()}",
            trial_end=subscription.trial_end.isoformat(),
        )
