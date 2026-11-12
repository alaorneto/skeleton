"""REST of transfers: request an export and follow it; the bytes are downloaded by the web route."""

from django.db import transaction
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError
from skeleton.api.openapi import DATETIME, INTEGER, UUID, Operation, nullable, obj, string
from skeleton.api.views import TenantAPI, page_numbers, request_fields

TRANSFER = obj(
    {
        "sysid": UUID,
        "kind": string(),
        "table_key": string(),
        "state": string(),
        "row_count": nullable(INTEGER),
        "size": nullable(INTEGER),
        "filename": string(),
        "error_code": string(),
        "created_at": DATETIME,
        "finished_at": nullable(DATETIME),
        "expires_at": DATETIME,
        "download_url": nullable(string()),
    }
)
EXPORT_INPUT = obj(
    {"table_key": string(), "filters": {"type": "object", "additionalProperties": {"type": "string"}}},
    required=("table_key",),
)


def transfer_data(info, organization_id):
    from django.urls import reverse

    data = {name: getattr(info, name) for name in TRANSFER["properties"] if name != "download_url"}
    data["download_url"] = (
        reverse("transfer:download", kwargs={"organization_id": organization_id, "sysid": info.sysid})
        if info.state == "succeeded"
        else None
    )
    return data


class ExportsView(TenantAPI):
    token_methods = ("POST",)
    openapi = {
        "POST": Operation(
            "Pede a exportação CSV dos registros que o ator lê",
            TRANSFER,
            status=202,
            request=EXPORT_INPUT,
            description=(
                "Os filtros são os da lista da tabela (sem paginação). O arquivo é gerado por um job; "
                "acompanhe o pedido em `transfers/` e baixe pelo `download_url` com a sessão."
            ),
        )
    }

    @transaction.atomic
    def post(self, request, organization_id):
        from skeleton.service_api.transfer import TransferService

        data = request_fields(request)
        if set(data) - {"table_key", "filters"} or not isinstance(data.get("table_key"), str):
            raise BoundaryError("invalid_request", "Envie table_key e, opcionalmente, filters.")
        filters = data.get("filters") or {}
        if not isinstance(filters, dict) or any(not isinstance(value, str) for value in filters.values()):
            raise BoundaryError("invalid_filter", "Os filtros são um objeto de textos.")
        key = request.headers.get("Idempotency-Key")
        info = TransferService().request_export(
            actor=request.user, table_key=data["table_key"], filters=tuple(filters.items()), idempotency_key=key
        )
        return Response(transfer_data(info, organization_id), status=202)


class TransfersView(TenantAPI):
    token_methods = ("GET",)
    openapi = {
        "GET": (
            Operation("Exportações do ator", TRANSFER, paginated=True, detail=False),
            Operation("Lê uma exportação do ator", TRANSFER, detail=True),
        )
    }

    def get(self, request, organization_id, sysid=None):
        from skeleton.service_api.transfer import TransferService

        service = TransferService()
        if sysid:
            return Response(transfer_data(service.transfer(actor=request.user, transfer_id=sysid), organization_id))
        if set(request.query_params) - {"page", "page_size"}:
            raise BoundaryError("invalid_filter", "Filtro não permitido.")
        from skeleton.service_api.transfer import _info
        from skeleton.transfer.exports import transfers_of

        number, size = page_numbers(request)
        rows = transfers_of(request.user)
        offset = (number - 1) * size
        return Response(
            {
                "count": rows.count(),
                "page": number,
                "page_size": size,
                "results": [transfer_data(_info(row), organization_id) for row in rows[offset : offset + size]],
            }
        )
