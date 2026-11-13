"""REST billing of the organization (/api/v1/o/<org>/billing/), over ``BillingService`` (#404).

``GET`` reads the standing, subscription, invoices and offers (``billing.view``); ``POST`` to
an action changes the subscription (``billing.manage``). Tokens never administer, so these
routes answer only sessions. The routes stay reachable while access is limited (the
``billing`` segment is exempt in the tenant middleware).
"""

from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from django.http import Http404
from django.utils.translation import gettext
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError
from skeleton.api.openapi import OBJECT, Operation, obj, string
from skeleton.api.views import TenantAPI, request_fields

ACTIONS = frozenset({"contract", "remove", "cancel", "resume", "voucher", "account", "checkout"})


def _jsonable(value):
    from datetime import datetime

    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _dto(value):
    return None if value is None else _jsonable(asdict(value))


def _uuid(data, name):
    try:
        return UUID(str(data.get(name)))
    except (TypeError, ValueError):
        raise BoundaryError("invalid_request", gettext("Identificador inválido."), 400) from None


class BillingAPI(TenantAPI):
    openapi = {
        "GET": Operation("Cobrança da organização", OBJECT, detail=False, capability="billing.view"),
    }

    def get(self, request, organization_id):
        from skeleton.service_api.billing import BillingService

        service = BillingService()
        actor = request.user
        return Response(
            {
                "access": _dto(service.access_state(actor=actor)),
                "subscription": _dto(service.subscription(actor=actor)),
                "invoices": [_dto(item) for item in service.list_invoices(actor=actor).items],
                "offers": [_dto(item) for item in service.offers(actor=actor)],
                "storage": _dto(service.storage_allowance(actor=actor)),
            }
        )


class BillingActionAPI(TenantAPI):
    openapi_detail = "action"
    openapi = {
        "POST": Operation(
            "Ação de cobrança (contratar, remover, cancelar, retomar, voucher, dados, pagar)",
            OBJECT,
            detail=True,
            request=obj({"price": string(), "item": string(), "code": string(), "invoice": string()}),
            capability="billing.manage",
        ),
    }

    def post(self, request, organization_id, action):
        from skeleton.service_api.billing import BillingService

        if action not in ACTIONS:
            raise Http404
        data = request_fields(request)
        service = BillingService()
        actor = request.user
        if action == "contract":
            result = service.contract(
                actor=actor, price_id=_uuid(data, "price"), terms_version=str(data.get("terms_version", ""))
            )
        elif action == "remove":
            result = service.remove_item(actor=actor, item_id=_uuid(data, "item"))
        elif action == "cancel":
            result = service.cancel(actor=actor)
        elif action == "resume":
            result = service.resume(actor=actor)
        elif action == "voucher":
            result = service.redeem_voucher(actor=actor, code=str(data.get("code", "")))
        elif action == "account":
            service.update_account(
                actor=actor,
                legal_name=str(data.get("legal_name", "")),
                billing_email=str(data.get("billing_email", "")),
                tax_id=str(data.get("tax_id", "")),
                country=str(data.get("country", "")),
            )
            return Response(status=204)
        else:
            link = service.checkout(
                actor=actor, invoice_id=_uuid(data, "invoice"), return_url=str(data.get("return_url", ""))
            )
            return Response({"url": link.url})
        return Response({"subscription": _dto(result)})
