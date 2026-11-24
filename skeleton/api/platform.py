"""The platform API (#709, #711): read-only views for the clients of the platform.

Every view is a ``PlatformAPI``: a ``platform`` token with the view's ``platform_scope``
(checked by ``ApiTokenMiddleware`` before the view runs, with the token's networks), or a
session of the platform administrator, for debugging. Anything else answers the neutral 404.
No view writes. Every answer is ``Cache-Control: no-store`` and every read is a security event
with the token prefix and the route name, never the parameters.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from django.conf import settings
from django.core import signing
from django.http import Http404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.authentication import SessionAuthentication
from rest_framework.response import Response
from rest_framework.views import APIView

from skeleton.common.logging import security_event
from skeleton.integrations.tokens.authentication import ApiTokenAuthentication

from .errors import BoundaryError
from .openapi import BOOLEAN, DATETIME, INTEGER, OBJECT, UUID, Operation, array, nullable, obj, string

TAG = "Plataforma"


class PlatformAPI(APIView):
    """Base of the platform views: ``platform_scope`` is required in every subclass."""

    authentication_classes = [ApiTokenAuthentication, SessionAuthentication]
    permission_classes: list[Any] = []
    platform_scope = ""
    token_methods = ("GET",)

    def check_permissions(self, request):
        from skeleton.accounts.platform import is_platform_admin

        token = getattr(request._request, "api_token", None)
        if token is not None and token.kind == "platform" and self.platform_scope in set(token.scopes):
            return
        if token is None and is_platform_admin(request.user):
            return
        raise Http404

    def has_scope(self, request, scope: str) -> bool:
        token = getattr(request._request, "api_token", None)
        if token is None:
            from skeleton.accounts.platform import is_platform_admin

            return is_platform_admin(request.user)
        return token.kind == "platform" and scope in set(token.scopes)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        token = getattr(request._request, "api_token", None)
        match = getattr(request._request, "resolver_match", None)
        security_event(
            "platform_api.read",
            reason_code=(match.url_name if match is not None else "platform")[:64],
            source=token.audit_source if token is not None else "http:session",
            status=response.status_code,
        )
        return response


def _now() -> str:
    return timezone.now().astimezone(UTC).isoformat()


APP = obj(
    {
        "app_label": string(),
        "app_id": nullable(string()),
        "version": nullable(string()),
        # "library" for a first-party library (#792): no availability nor limits of its own.
        "kind": string(),
        "availability_mode": nullable(string()),
        "limits": OBJECT,
    }
)
INSTANCE = obj(
    {
        "instance_id": UUID,
        "name": string(),
        "purpose": string(),
        "product_name": string(),
        "core_version": nullable(string()),
        "billing_enabled": BOOLEAN,
        "apps": array(APP),
        "generated_at": DATETIME,
    }
)


def _scope_note(scope: str) -> str:
    return f"Escopo exigido: `{scope}` (token de plataforma)."


class InstanceView(PlatformAPI):
    """Identity, version and installed apps of the instance (``platform.instance:read``)."""

    platform_scope = "platform.instance:read"
    openapi = {"GET": Operation("Identidade e apps da instância", INSTANCE, description=_scope_note(platform_scope))}

    def get(self, request):
        from skeleton.system.app_registry import all_apps
        from skeleton.system.instance_identity import instance_identity
        from skeleton.system.models import AppAvailability
        from skeleton.versioning import core_version

        identity = instance_identity()
        rows = {row.app_label: row for row in AppAvailability.objects.all()}
        apps = []
        for manifest in sorted(all_apps(), key=lambda item: item.app_label):
            row = None if manifest.library else rows.get(manifest.app_label)
            apps.append(
                {
                    "app_label": manifest.app_label,
                    "app_id": manifest.app_id,
                    "version": manifest.version,
                    "kind": "library" if manifest.library else "app",
                    "availability_mode": row.mode if row is not None else None,
                    "limits": dict(getattr(row, "limits", None) or {}) if row is not None else {},
                }
            )
        return Response(
            {
                "instance_id": str(identity.instance_id),
                "name": identity.name,
                "purpose": identity.purpose,
                "product_name": str(getattr(settings, "SKELETON_PRODUCT_NAME", "") or "Skeleton"),
                "core_version": core_version()["version"],
                "billing_enabled": bool(getattr(settings, "SKELETON_BILLING_ENABLED", False)),
                "apps": apps,
                "generated_at": _now(),
            }
        )


# --------------------------------------------------------------------------- #711: organizations, billing, metrics

_CURSOR_SALT = "skeleton.platform-api.cursor"
MAX_PAGE = 200
DEFAULT_PAGE = 100


def _encode_cursor(updated_at: datetime, sysid: Any) -> str:
    signed = signing.dumps({"u": updated_at.isoformat(), "s": str(sysid)}, salt=_CURSOR_SALT, compress=False)
    return base64.urlsafe_b64encode(signed.encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        data = signing.loads(base64.urlsafe_b64decode(padded.encode()).decode(), salt=_CURSOR_SALT)
        moment = parse_datetime(data["u"])
        if moment is None:
            raise ValueError
        return moment, str(data["s"])
    except (signing.BadSignature, ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise BoundaryError("invalid_cursor", "Cursor inválido.") from None


def _flag(value: str | None, name: str) -> bool | None:
    if value is None:
        return None
    if value in ("true", "false"):
        return value == "true"
    raise BoundaryError("invalid_filter", f"Use true ou false em {name}.")


def _only(request, allowed: set[str]) -> None:
    unknown = set(request.query_params) - allowed
    if unknown:
        raise BoundaryError("invalid_filter", "Parâmetro não aceito: " + ", ".join(sorted(unknown)) + ".")


def _page_size(request) -> int:
    raw = request.query_params.get("page_size")
    if raw is None:
        return DEFAULT_PAGE
    try:
        value = int(raw)
    except ValueError:
        raise BoundaryError("invalid_filter", "page_size inválido.") from None
    if not 1 <= value <= MAX_PAGE:
        raise BoundaryError("invalid_filter", f"page_size de 1 a {MAX_PAGE}.")
    return value


def organization_item(row: Any, *, billing: bool) -> dict[str, Any]:
    item = {
        "id": str(row.subject_organization_id),
        "name": row.name,
        "slug": row.slug,
        "is_active": row.is_active,
        "created_at": row.organization_created_at.isoformat(),
        "members": {
            "active": row.members_active,
            "limit": row.members_limit,
            "external": row.external_people,
            "service_accounts": row.service_accounts,
        },
        "apps": list(row.apps_effective),
        "bundle": row.bundle_code or None,
        "storage": {
            "used_bytes": row.storage_bytes_used,
            "quota_bytes": row.storage_quota_bytes,
            "location": row.storage_location_code or None,
        },
        "refreshed_at": row.refreshed_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }
    if billing:
        item["billing"] = dict(row.billing or {})
    return item


MEMBERS = obj({"active": INTEGER, "limit": nullable(INTEGER), "external": INTEGER, "service_accounts": INTEGER})
STORAGE = obj({"used_bytes": INTEGER, "quota_bytes": nullable(INTEGER), "location": nullable(string())})
ORGANIZATION = obj(
    {
        "id": UUID,
        "name": string(),
        "slug": string(),
        "is_active": BOOLEAN,
        "created_at": DATETIME,
        "members": MEMBERS,
        "apps": array(string()),
        "bundle": nullable(string()),
        "storage": STORAGE,
        "billing": OBJECT,
        "refreshed_at": DATETIME,
        "updated_at": DATETIME,
    },
    # ``billing`` only with ``platform.billing:read``.
    required=(
        "id",
        "name",
        "slug",
        "is_active",
        "created_at",
        "members",
        "apps",
        "bundle",
        "storage",
        "refreshed_at",
        "updated_at",
    ),
)
ORGANIZATION_PAGE = obj({"results": array(ORGANIZATION), "next_cursor": nullable(string()), "generated_at": DATETIME})


class OrganizationsView(PlatformAPI):
    """The summary of every organization, paged by an opaque signed cursor (``platform.organizations:read``)."""

    platform_scope = "platform.organizations:read"
    openapi = {
        "GET": Operation(
            "Resumo das organizações",
            ORGANIZATION_PAGE,
            description=_scope_note(platform_scope)
            + " Filtros: updated_since, is_active, access_state, has_overdue, cursor, page_size (1 a 200). "
            "`billing` só com `platform.billing:read`.",
        )
    }

    def get(self, request):
        from django.db.models import Q

        from skeleton.insights.models import OrganizationSnapshot

        _only(request, {"updated_since", "is_active", "access_state", "has_overdue", "cursor", "page_size"})
        rows = OrganizationSnapshot.objects.all()
        since = request.query_params.get("updated_since")
        if since is not None:
            moment = parse_datetime(since)
            if moment is None or timezone.is_naive(moment):
                raise BoundaryError("invalid_filter", "updated_since: data e hora ISO 8601 com fuso.")
            rows = rows.filter(updated_at__gt=moment)
        active = _flag(request.query_params.get("is_active"), "is_active")
        if active is not None:
            rows = rows.filter(is_active=active)
        state = request.query_params.get("access_state")
        if state is not None:
            rows = rows.filter(billing__access_state=state)
        if _flag(request.query_params.get("has_overdue"), "has_overdue"):
            rows = rows.filter(billing__overdue_amount__gt=0)
        cursor = request.query_params.get("cursor")
        if cursor:
            moment, sysid = _decode_cursor(cursor)
            rows = rows.filter(Q(updated_at__gt=moment) | Q(updated_at=moment, sysid__gt=sysid))
        size = _page_size(request)
        page = list(rows.order_by("updated_at", "sysid")[: size + 1])
        more = len(page) > size
        page = page[:size]
        billing = self.has_scope(request, "platform.billing:read")
        return Response(
            {
                "results": [organization_item(row, billing=billing) for row in page],
                "next_cursor": _encode_cursor(page[-1].updated_at, page[-1].sysid) if more and page else None,
                "generated_at": _now(),
            }
        )


def _snapshot(organization_id: Any) -> Any:
    from skeleton.insights.models import OrganizationSnapshot

    row = OrganizationSnapshot.objects.filter(subject_organization_id=organization_id).first()
    if row is None:
        raise Http404
    return row


class OrganizationView(PlatformAPI):
    platform_scope = "platform.organizations:read"
    openapi = {"GET": Operation("Resumo de uma organização", ORGANIZATION, description=_scope_note(platform_scope))}

    def get(self, request, sysid):
        organization_id = sysid
        _only(request, set())
        return Response(
            organization_item(_snapshot(organization_id), billing=self.has_scope(request, "platform.billing:read"))
        )


def _billing_context(organization_id: Any):
    """The read-only system context of one organization for a platform token with the billing scope."""
    from contextlib import ExitStack

    from skeleton.access.administration import read_only_inspection
    from skeleton.tenancy.context import system_context

    stack = ExitStack()
    stack.enter_context(system_context(reason="platform:api", organization_id=organization_id))
    stack.enter_context(read_only_inspection("default"))
    return stack


INVOICE = obj(
    {
        "id": UUID,
        "number": nullable(string()),
        "status": string(),
        "currency": string(),
        "subtotal": INTEGER,
        "discount": INTEGER,
        "tax": INTEGER,
        "total": INTEGER,
        "paid": INTEGER,
        "period_start": nullable(DATETIME),
        "period_end": nullable(DATETIME),
        "issued_at": nullable(DATETIME),
        "due_at": nullable(DATETIME),
        "paid_at": nullable(DATETIME),
    }
)
PAYMENT = obj(
    {
        "id": UUID,
        "invoice_id": nullable(UUID),
        "method": string(),
        "status": string(),
        "amount": INTEGER,
        "currency": string(),
        "provider": string(),
        "created_at": DATETIME,
        "confirmed_at": nullable(DATETIME),
    }
)


def _list_page(request, items: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        number = int(request.query_params.get("page", "1"))
    except ValueError:
        raise BoundaryError("invalid_filter", "page inválido.") from None
    size = _page_size(request)
    if number < 1:
        raise BoundaryError("invalid_filter", "page inválido.")
    start = (number - 1) * size
    return {"results": items[start : start + size], "count": len(items), "page": number, "page_size": size}


class OrganizationInvoicesView(PlatformAPI):
    platform_scope = "platform.billing:read"
    openapi = {
        "GET": Operation(
            "Faturas de uma organização",
            obj({"results": array(INVOICE), "count": INTEGER, "page": INTEGER, "page_size": INTEGER}),
            description=_scope_note(platform_scope) + " Filtros: status, issued_since, page, page_size.",
        )
    }

    def get(self, request, sysid):
        organization_id = sysid
        from skeleton.billing.reporting import organization_invoices

        _only(request, {"status", "issued_since", "page", "page_size"})
        _snapshot(organization_id)
        since = request.query_params.get("issued_since")
        moment = None
        if since is not None:
            moment = parse_datetime(since)
            if moment is None or timezone.is_naive(moment):
                raise BoundaryError("invalid_filter", "issued_since: data e hora ISO 8601 com fuso.")
        with _billing_context(organization_id):
            items = organization_invoices(status=request.query_params.get("status"), issued_since=moment)
        return Response(_list_page(request, items))


class OrganizationPaymentsView(PlatformAPI):
    platform_scope = "platform.billing:read"
    openapi = {
        "GET": Operation(
            "Pagamentos de uma organização",
            obj({"results": array(PAYMENT), "count": INTEGER, "page": INTEGER, "page_size": INTEGER}),
            description=_scope_note(platform_scope) + " Nunca a referência do provedor nem dados do meio de pagamento.",
        )
    }

    def get(self, request, sysid):
        organization_id = sysid
        from skeleton.billing.reporting import organization_payments

        _only(request, {"page", "page_size"})
        _snapshot(organization_id)
        with _billing_context(organization_id):
            items = organization_payments()
        return Response(_list_page(request, items))


BILLING_ACCOUNT = obj(
    {
        "legal_name": string(),
        "billing_email": string(),
        "tax_id": string(),
        "country": string(),
        "currency": string(),
        "access_state": string(),
    }
)


class OrganizationBillingAccountView(PlatformAPI):
    """The billing contact of one organization: personal data (``platform.billing_contacts:read``)."""

    platform_scope = "platform.billing_contacts:read"
    openapi = {
        "GET": Operation(
            "Contato de cobrança de uma organização",
            nullable(BILLING_ACCOUNT),
            description=_scope_note(platform_scope) + " Dado pessoal: cada leitura é registrada.",
        )
    }

    def get(self, request, sysid):
        organization_id = sysid
        from skeleton.billing.reporting import organization_billing_account

        _only(request, set())
        _snapshot(organization_id)
        with _billing_context(organization_id):
            account = organization_billing_account()
        token = getattr(request._request, "api_token", None)
        security_event(
            "platform_api.personal_data",
            reason_code="billing_account",
            record_sysid=str(organization_id),
            source=token.audit_source if token is not None else "http:session",
        )
        if account is None:
            raise Http404
        return Response(account)


METRIC = obj({"day": string(), "code": string(), "currency": string(), "value": INTEGER})
MAX_METRIC_DAYS = 400


class MetricsView(PlatformAPI):
    platform_scope = "platform.metrics:read"
    openapi = {
        "GET": Operation(
            "Métricas diárias da instância",
            obj({"results": array(METRIC)}),
            description=_scope_note(platform_scope)
            + " Filtros: since, until (datas; até 400 dias), code (repetível; `prefixo*`), currency.",
        )
    }

    def get(self, request):
        from django.db.models import Q

        from skeleton.insights.models import InstanceMetric

        _only(request, {"since", "until", "code", "currency"})
        today = timezone.now().astimezone(UTC).date()
        until = parse_date(request.query_params.get("until", "")) if "until" in request.query_params else today
        since = (
            parse_date(request.query_params.get("since", ""))
            if "since" in request.query_params
            else until - timedelta(days=30)
            if until
            else None
        )
        if since is None or until is None or since > until:
            raise BoundaryError("invalid_filter", "since e until: datas AAAA-MM-DD, since antes de until.")
        if (until - since).days > MAX_METRIC_DAYS:
            raise BoundaryError("invalid_filter", f"Intervalo de até {MAX_METRIC_DAYS} dias.")
        rows = InstanceMetric.objects.filter(day__gte=since, day__lte=until)
        codes = request.query_params.getlist("code")
        if codes:
            condition = Q()
            for code in codes:
                condition |= Q(code__startswith=code[:-1]) if code.endswith("*") else Q(code=code)
            rows = rows.filter(condition)
        currency = request.query_params.get("currency")
        if currency is not None:
            rows = rows.filter(currency=currency)
        return Response(
            {
                "results": [
                    {"day": row.day.isoformat(), "code": row.code, "currency": row.currency, "value": row.value}
                    for row in rows.order_by("day", "code", "currency")
                ]
            }
        )
