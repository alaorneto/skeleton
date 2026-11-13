"""Platform billing (/platform/billing/, #405): catalog, vouchers, provider accounts and accounts.

Explicit views over ``skeleton.billing.platform``. The catalog pages accept the platform
administrator and the limited ``billing`` role (``billing.operate``); provider secrets need a
recent authentication and are never shown back. One organization's billing page and its
actions (offline payment, void, access extension, courtesy) are for the administrator, and
every action on an account takes a reason.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.http import require_POST, require_safe

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.billing import money
from skeleton.billing import platform as operations
from skeleton.common.exceptions import ConcurrentUpdate

from .guards import platform_page

CAPABILITY = operations.BILLING_OPERATE
ORGANIZATION_ACTIONS = frozenset({"paid", "void", "extend", "courtesy"})


def _webhook_url(request, account):
    path = reverse("billing_webhooks:receive", kwargs={"provider": account.provider, "account_id": account.pk})
    origin = str(getattr(settings, "SKELETON_PUBLIC_URL", "") or "").rstrip("/")
    return f"{origin}{path}" if origin else request.build_absolute_uri(path)


def _catalog_page(request, *, status=200, error=None):
    from skeleton.billing.models import Price, Product, ProviderAccount, Voucher
    from skeleton.billing.providers.base import all_providers

    accounts = list(ProviderAccount.objects.order_by("provider", "label"))
    return render(
        request,
        "skeleton/web/platform/billing.html",
        {
            "platform_section": "billing",
            "products": list(Product.objects.order_by("kind", "code")),
            "prices": list(
                Price.objects.select_related("product").order_by("product__code", "currency", "unit_amount")
            ),
            "vouchers": list(Voucher.objects.order_by("-created_at")[:100]),
            "accounts": [(account, _webhook_url(request, account)) for account in accounts],
            "providers": all_providers(),
            "error": error,
        },
        status=status,
    )


@platform_page(capability=CAPABILITY)
@require_safe
def billing(request):
    return _catalog_page(request)


def _flag(data, name):
    return data.get(name) in ("on", "true", "1")


def _int(data, name, default=0):
    value = str(data.get(name, "") or "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise ValidationError({name: gettext("Use um número inteiro.")}) from None


def _gigabytes(data, name):
    value = str(data.get(name, "") or "").strip().replace(",", ".")
    if not value:
        return 0
    try:
        from decimal import Decimal

        return int(Decimal(value) * 1024**3)
    except Exception:  # noqa: BLE001 - any malformed number is the same invalid field
        raise ValidationError({name: gettext("Use um número.")}) from None


def _json(data, name, default):
    text = str(data.get(name, "") or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        raise ValidationError({name: gettext("Use JSON válido.")}) from None


def _catalog_action(request, action):
    try:
        with transaction.atomic():
            action()
    except (ValidationError, ConcurrentUpdate) as error:
        detail = (
            "; ".join(f"{key}: {', '.join(values)}" for key, values in error.message_dict.items())
            if isinstance(error, ValidationError) and hasattr(error, "error_dict")
            else gettext("Os dados mudaram ou são inválidos. Revise e tente de novo.")
        )
        return _catalog_page(request, status=400, error=detail)
    messages.success(request, gettext("Catálogo de cobrança atualizado."))
    return redirect("platform:billing")


@platform_page(capability=CAPABILITY, writes=True)
@require_POST
def billing_product(request):
    data = request.POST
    return _catalog_action(
        request,
        lambda: operations.create_product(
            request.user,
            code=str(data.get("code", "")),
            kind=str(data.get("kind", "")),
            name=str(data.get("name", "")),
            app_label=str(data.get("app_label", "")),
            is_public=_flag(data, "is_public"),
        ),
    )


@platform_page(capability=CAPABILITY, writes=True)
@require_POST
def billing_price(request):
    from skeleton.billing.models import Product

    data = request.POST
    product = get_object_or_404(Product.objects, pk=data.get("product"))

    def create():
        currency = str(data.get("currency", "")).strip().upper()
        overage = str(data.get("storage_overage", "block"))
        operations.create_price(
            request.user,
            product,
            currency=currency,
            unit_amount=money.parse(str(data.get("amount", "")), currency),
            interval=str(data.get("interval", "month")),
            interval_count=_int(data, "interval_count", 1),
            trial_days=_int(data, "trial_days"),
            included_storage_bytes=_gigabytes(data, "included_storage_gb"),
            included_apps=_json(data, "included_apps", []),
            limits=_json(data, "limits", {}),
            storage_overage=overage,
            overage_unit_bytes=_gigabytes(data, "overage_unit_gb") or None,
            overage_unit_amount=money.parse(str(data.get("overage_unit_amount")), currency)
            if data.get("overage_unit_amount")
            else None,
            overage_hard_cap_bytes=_gigabytes(data, "overage_hard_cap_gb") or None,
        )

    return _catalog_action(request, create)


@platform_page(capability=CAPABILITY, writes=True)
@require_POST
def billing_price_flags(request, sysid):
    from skeleton.billing.models import Price

    price = get_object_or_404(Price.objects, pk=sysid)
    data = request.POST
    return _catalog_action(
        request,
        lambda: operations.set_price_flags(
            request.user,
            price,
            is_active=_flag(data, "is_active"),
            is_default=_flag(data, "is_default"),
            expected_revision=_int(data, "revision"),
        ),
    )


@platform_page(capability=CAPABILITY, writes=True)
@require_POST
def billing_voucher(request):
    data = request.POST

    def create():
        kind = str(data.get("kind", ""))
        currency = str(data.get("currency", "") or "").strip().upper()
        value = (
            money.parse(str(data.get("value", "")), currency)
            if kind == "fixed"
            else (
                int(round(float(str(data.get("value") or 0).replace(",", ".")) * 100))
                if kind == "percent"
                else _int(data, "value")
            )
        )
        until = str(data.get("valid_until", "") or "").strip()
        operations.create_voucher(
            request.user,
            code=str(data.get("code", "")),
            kind=kind,
            value=value,
            currency=currency if kind == "fixed" else "",
            duration=str(data.get("duration", "once")),
            duration_cycles=_int(data, "duration_cycles") or None,
            applies_to=[code.strip() for code in str(data.get("applies_to", "")).split(",") if code.strip()],
            max_redemptions=_int(data, "max_redemptions") or None,
            valid_until=datetime.fromisoformat(until).replace(tzinfo=UTC) if until else None,
        )

    return _catalog_action(request, create)


@platform_page(capability=CAPABILITY, writes=True)
@require_POST
def billing_voucher_toggle(request, sysid):
    from skeleton.billing.models import Voucher

    voucher = get_object_or_404(Voucher.objects, pk=sysid)
    return _catalog_action(
        request,
        lambda: operations.set_voucher_active(
            request.user,
            voucher,
            active=_flag(request.POST, "active"),
            expected_revision=_int(request.POST, "revision"),
        ),
    )


@platform_page(capability=CAPABILITY, writes=True)
@recent_authentication_required()
@require_POST
def billing_provider(request, sysid=None):
    from skeleton.billing.models import ProviderAccount

    account = get_object_or_404(ProviderAccount.objects, pk=sysid) if sysid else None
    data = request.POST
    return _catalog_action(
        request,
        lambda: operations.save_provider_account(
            request.user,
            account,
            provider=account.provider if account else str(data.get("provider", "")),
            label=str(data.get("label", "")),
            mode=str(data.get("mode", "test")),
            currencies=[code.strip().upper() for code in str(data.get("currencies", "")).split(",") if code.strip()],
            enabled=_flag(data, "enabled"),
            is_default=_flag(data, "is_default"),
            api_key=str(data.get("api_key", "")),
            webhook_secret=str(data.get("webhook_secret", "")),
            expected_revision=_int(data, "revision") if account else None,
        ),
    )


# One organization -------------------------------------------------------------------------------


def _organization(sysid):
    from skeleton.tenancy.models import Organization

    return get_object_or_404(Organization.objects, pk=sysid)


def _organization_page(request, organization, *, status=200, error=None):
    from skeleton.billing.models import Price

    snapshot = operations.account_snapshot(request.user, organization.pk)
    return render(
        request,
        "skeleton/web/platform/organization_billing.html",
        {
            "platform_section": "organizations",
            "organization": organization,
            "prices": list(Price.objects.select_related("product").filter(is_active=True).order_by("product__code")),
            "error": error,
            **snapshot,
        },
        status=status,
    )


@platform_page
@require_safe
def organization_billing(request, sysid):
    return _organization_page(request, _organization(sysid))


@platform_page
@require_POST
def organization_billing_action(request, sysid, action):
    from skeleton.billing.models import Price

    if action not in ORGANIZATION_ACTIONS:
        raise Http404
    organization = _organization(sysid)
    data = request.POST
    reason = str(data.get("reason", ""))
    try:
        with transaction.atomic():
            if action == "paid":
                operations.mark_paid_offline(
                    request.user, organization.pk, UUID(str(data.get("invoice"))), reason=reason
                )
            elif action == "void":
                operations.void(request.user, organization.pk, UUID(str(data.get("invoice"))), reason=reason)
            elif action == "extend":
                until = str(data.get("until", "") or "").strip()
                operations.extend_access(
                    request.user,
                    organization.pk,
                    until=datetime.fromisoformat(until).replace(tzinfo=UTC) if until else None,
                    reason=reason,
                )
            else:
                price = get_object_or_404(Price.objects.select_related("product"), pk=data.get("price"))
                operations.grant_courtesy(request.user, organization.pk, price, reason=reason)
    except (ValidationError, ValueError) as error:
        detail = (
            "; ".join(", ".join(values) for values in error.message_dict.values())
            if isinstance(error, ValidationError) and hasattr(error, "error_dict")
            else gettext("Verifique os dados informados.")
        )
        return _organization_page(request, organization, status=400, error=detail)
    except PermissionDenied:
        raise
    messages.success(request, gettext("Cobrança da organização atualizada."))
    return redirect("platform:organization-billing", sysid=organization.pk)
