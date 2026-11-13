"""The organization's billing pages (/o/<org>/billing/), over ``BillingService`` (#404).

Reading needs ``billing.view``; every change needs ``billing.manage``. The pages stay
reachable while the organization's access is limited (the ``billing`` segment is exempt in
the tenant middleware), because they are how the account is settled. Paying an invoice
redirects to the provider's hosted page; it runs without the organization lock, so no row is
locked while the provider is called.
"""

from __future__ import annotations

from uuid import UUID

from django.contrib import messages
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.http import require_POST, require_safe

from skeleton.access.capabilities import BILLING_MANAGE, BILLING_VIEW, has_capability
from skeleton.service_api import errors as service_errors
from skeleton.service_api.billing import BillingService
from skeleton.web.views import tenant_page

TERMS_VERSION = "1"
ACTIONS = frozenset({"contract", "remove", "cancel", "resume", "voucher", "account"})


def _overview_url(organization_id):
    return reverse("billing-overview", kwargs={"organization_id": organization_id})


def _usage_by_app():
    from skeleton.billing.models import UsageSnapshot

    snapshot = UsageSnapshot.objects.order_by("-day").first()
    return sorted((snapshot.by_app or {}).items()) if snapshot else []


@tenant_page(capability=BILLING_VIEW)
@require_safe
def billing_overview(request, organization_id):
    from skeleton.billing import engine

    service = BillingService()
    actor = request.user
    subscription = service.subscription(actor=actor)
    account = engine.account()
    return render(
        request,
        "skeleton/web/billing.html",
        {
            "organization_id": organization_id,
            "access": service.access_state(actor=actor),
            "subscription": subscription,
            "invoices": service.list_invoices(actor=actor).items,
            "offers": service.offers(actor=actor),
            "storage": service.storage_allowance(actor=actor),
            "usage_by_app": _usage_by_app(),
            "account": account,
            "can_manage": has_capability(actor, BILLING_MANAGE),
            "terms_version": TERMS_VERSION,
        },
    )


def _uuid(value):
    try:
        return UUID(str(value))
    except ValueError:
        raise Http404 from None


@tenant_page(capability=BILLING_MANAGE)
@require_POST
def billing_action(request, organization_id, action):
    if action not in ACTIONS:
        raise Http404
    service = BillingService()
    actor = request.user
    data = request.POST
    try:
        if action == "contract":
            if data.get("accept_terms") != "on":
                messages.error(request, gettext("Aceite os termos de uso para contratar."))
                return redirect(_overview_url(organization_id))
            service.contract(actor=actor, price_id=_uuid(data.get("price")), terms_version=TERMS_VERSION)
            messages.success(request, gettext("Contratação registrada. O acesso é liberado com o pagamento."))
        elif action == "remove":
            service.remove_item(actor=actor, item_id=_uuid(data.get("item")))
            messages.success(request, gettext("O item termina no fim do período."))
        elif action == "cancel":
            service.cancel(actor=actor)
            messages.success(request, gettext("A assinatura será cancelada no fim do período."))
        elif action == "resume":
            service.resume(actor=actor)
            messages.success(request, gettext("O cancelamento foi desfeito."))
        elif action == "voucher":
            service.redeem_voucher(actor=actor, code=str(data.get("code", "")))
            messages.success(request, gettext("Voucher aplicado."))
        else:
            service.update_account(
                actor=actor,
                legal_name=str(data.get("legal_name", "")),
                billing_email=str(data.get("billing_email", "")),
                tax_id=str(data.get("tax_id", "")),
                country=str(data.get("country", "")),
            )
            messages.success(request, gettext("Dados de cobrança salvos."))
    except service_errors.InvalidInput as error:
        detail = next(iter(next(iter(error.errors.values()), ())), gettext("Verifique os campos informados."))
        messages.error(request, detail)
    except service_errors.Conflict as error:
        if error.code == "organization_restricted":
            messages.error(request, gettext("Regularize a cobrança antes de contratar."))
        else:
            messages.error(request, gettext("A operação conflita com o estado atual."))
    return redirect(_overview_url(organization_id))


@tenant_page()
@require_POST
def billing_pay(request, organization_id, invoice_id):
    """Redirect to the provider's hosted page; no organization lock is held during the call."""
    from django.conf import settings

    path = _overview_url(organization_id) + "?returned=1"
    origin = str(getattr(settings, "SKELETON_PUBLIC_URL", "") or "").rstrip("/")
    return_url = f"{origin}{path}" if origin else request.build_absolute_uri(path)
    try:
        link = BillingService().checkout(actor=request.user, invoice_id=invoice_id, return_url=return_url)
    except service_errors.InvalidInput:
        messages.error(request, gettext("O pagamento não está disponível agora. Tente de novo mais tarde."))
        return redirect(_overview_url(organization_id))
    return HttpResponseRedirect(link.url)
