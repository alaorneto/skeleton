"""REST for join requests (#26): the requester under ``/me/``, deciders under ``/o/<org>/``."""

from __future__ import annotations

import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from rest_framework.response import Response

from skeleton.access.capabilities import ADMISSION_DECIDE, require_resource_capability
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy import admission

from .errors import BoundaryError
from .openapi import DATETIME, INTEGER, UUID, Operation, nullable, obj, results, string
from .views import SessionAPI, TenantAPI, etag, request_fields

_REQUEST_FIELDS = {
    "sysid": UUID,
    "organization_id": UUID,
    "channel": string(),
    "status": string(),
    "message": string(),
    "created_at": DATETIME,
    "expires_at": DATETIME,
    "decided_at": nullable(DATETIME),
    "revision": INTEGER,
}
MY_JOIN_REQUEST = obj({**_REQUEST_FIELDS, "organization_name": string()})
NAMED = obj({"sysid": UUID, "name": string()})
DECIDER_JOIN_REQUEST = obj({**_REQUEST_FIELDS, "role": NAMED, "user": obj({"sysid": UUID, "email": string()})})
JOIN_REQUEST_INPUT = obj({"organization_id": UUID, "message": string()}, required=("organization_id",))
DECISION_INPUT = obj({"decision": string(enum=["approve", "reject"])})
_LINK_FIELDS = {
    "sysid": UUID,
    "label": string(),
    "role": NAMED,
    "mode": string(),
    "expires_at": DATETIME,
    "max_uses": INTEGER,
    "use_count": INTEGER,
    "revision": INTEGER,
}
JOIN_LINK = obj(_LINK_FIELDS)
CREATED_JOIN_LINK = obj({**_LINK_FIELDS, "url": string(format="uri")})
JOIN_LINK_INPUT = obj({"label": string(), "role": UUID, "mode": string(), "days": INTEGER, "max_uses": INTEGER})
REVOKED_JOIN_LINK = obj({"sysid": UUID, "revoked": {"const": True}})


def _revision(request):
    value = request.headers.get("If-Match")
    if value is None:
        raise BoundaryError("revision_required", "Informe a revisão.", 428)
    if not re.fullmatch(r'"(?:0|[1-9][0-9]{0,18})"', value):
        raise BoundaryError("invalid_revision", "Revisão inválida.")
    return int(value[1:-1])


def request_data(record, *, decider=False):
    data = {
        "sysid": str(record.pk),
        "organization_id": str(record.organization_id),
        "channel": record.channel,
        "status": admission.effective_status(record),
        "message": record.message,
        "created_at": record.created_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
        "decided_at": record.decided_at.isoformat() if record.decided_at else None,
        "revision": record.revision,
    }
    if decider:
        # Roles are tenant rows: only the decider, inside the organization, reads the role.
        data["role"] = {"sysid": str(record.role_id), "name": record.role.name}
        data["user"] = {"sysid": str(record.user_id), "email": record.user.email}
    else:
        data["organization_name"] = record.organization.name
    return data


def _stale():
    return BoundaryError("revision_conflict", "O pedido foi alterado por outra operação.", 412)


class MyJoinRequestsAPI(SessionAPI):
    """``GET`` lists the person's requests in every organization; ``POST`` asks to join one."""

    openapi = {
        "GET": Operation("Pedidos de ingresso do ator em todas as organizações", results(MY_JOIN_REQUEST)),
        "POST": Operation(
            "Pede ingresso numa organização",
            MY_JOIN_REQUEST,
            status=201,
            etag=True,
            request=JOIN_REQUEST_INPUT,
            extra_responses={"429": "Limite de pedidos; veja Retry-After."},
        ),
    }

    def get(self, request):
        return Response({"results": [request_data(item) for item in admission.my_join_requests(request.user)]})

    def post(self, request):
        data = request_fields(request)
        if set(data) - {"organization_id", "message"} or "organization_id" not in data:
            raise BoundaryError("invalid_request", "Informe organization_id e, opcionalmente, message.")
        try:
            with transaction.atomic():
                record = admission.submit_join_request(
                    user=request.user, organization_id=data["organization_id"], message=data.get("message", "")
                )
        except admission.JoinRateLimited as limited:
            error = BoundaryError("rate_limited", "Muitas solicitações. Tente de novo mais tarde.", 429)
            response = Response({"code": error.code, "detail": error.detail}, status=429)
            response["Retry-After"] = str(limited.retry_after)
            return response
        except admission.JoinUnavailable:
            raise BoundaryError("not_found", "Not found.", 404) from None
        except admission.AlreadyMember:
            raise BoundaryError("already_member", "Você já é membro desta organização.", 409) from None
        except PermissionDenied:
            raise BoundaryError("email_unverified", "Confirme seu email para pedir ingresso.", 403) from None
        return etag(Response(request_data(record), status=201), record.revision)


class MyJoinRequestWithdrawAPI(SessionAPI):
    openapi = {"POST": Operation("Retira um pedido de ingresso aberto", MY_JOIN_REQUEST, etag=True, if_match=True)}

    def post(self, request, sysid):
        revision = _revision(request)
        try:
            with transaction.atomic():
                record = admission.withdraw_join_request(
                    user=request.user, join_request_id=sysid, expected_revision=revision
                )
        except admission.JoinUnavailable:
            raise Http404 from None
        except ConcurrentUpdate:
            raise _stale() from None
        except ValidationError:
            raise BoundaryError("not_open", "O pedido não está mais aberto.", 409) from None
        return etag(Response(request_data(record)), record.revision)


class OrganizationJoinRequestsAPI(TenantAPI):
    """Deciders list open requests and decide one with ``If-Match``."""

    openapi = {
        "GET": (
            Operation(
                "Pedidos de ingresso pendentes",
                results(DECIDER_JOIN_REQUEST),
                detail=False,
                capability=ADMISSION_DECIDE,
            ),
            Operation(
                "Um pedido de ingresso pendente",
                DECIDER_JOIN_REQUEST,
                etag=True,
                detail=True,
                capability=ADMISSION_DECIDE,
            ),
        ),
        "POST": Operation(
            "Aprova ou recusa um pedido de ingresso",
            DECIDER_JOIN_REQUEST,
            etag=True,
            if_match=True,
            request=DECISION_INPUT,
            detail=True,
            capability=ADMISSION_DECIDE,
        ),
    }

    def check_permissions(self, request):
        super().check_permissions(request)
        require_resource_capability(request.user, ADMISSION_DECIDE)

    def get(self, request, organization_id, sysid=None):
        if sysid is None:
            rows = admission.pending_requests()[:200]
            return Response({"results": [request_data(item, decider=True) for item in rows]})
        record = admission.pending_requests().filter(pk=sysid).first()
        if record is None:
            raise Http404
        return etag(Response(request_data(record, decider=True)), record.revision)

    def post(self, request, organization_id, sysid=None):
        if sysid is None:
            raise Http404
        revision = _revision(request)
        data = request_fields(request)
        if set(data) != {"decision"} or data["decision"] not in ("approve", "reject"):
            raise BoundaryError("invalid_request", 'Informe decision: "approve" ou "reject".')
        try:
            with transaction.atomic():
                record = admission.decide_join_request(
                    actor=request.user,
                    join_request_id=sysid,
                    approve=data["decision"] == "approve",
                    expected_revision=revision,
                    request=request,
                )
        except admission.JoinUnavailable:
            raise Http404 from None
        except ConcurrentUpdate:
            raise _stale() from None
        except ValidationError:
            raise BoundaryError("not_pending", "O pedido não está mais pendente.", 409) from None
        return etag(Response(request_data(record, decider=True)), record.revision)


# --------------------------------------------------------------------------- join links (#27)


def link_data(link):
    """Never the digest; the token exists only in the creation response."""
    return {
        "sysid": str(link.pk),
        "label": link.label,
        "role": {"sysid": str(link.role_id), "name": link.role.name},
        "mode": link.mode,
        "expires_at": link.expires_at.isoformat(),
        "max_uses": link.max_uses,
        "use_count": link.use_count,
        "revision": link.revision,
    }


class OrganizationJoinLinksAPI(TenantAPI):
    """``invitations.manage`` lists and creates links; revoking takes ``If-Match``."""

    openapi = {
        "GET": Operation("Links de ingresso vivos", results(JOIN_LINK), capability="invitations.manage"),
        "POST": Operation(
            "Cria um link de ingresso",
            CREATED_JOIN_LINK,
            status=201,
            etag=True,
            request=JOIN_LINK_INPUT,
            capability="invitations.manage",
            description="O token só aparece em url, nesta resposta, que não vai para cache.",
        ),
    }

    def check_permissions(self, request):
        from skeleton.access.capabilities import INVITATIONS_MANAGE

        super().check_permissions(request)
        require_resource_capability(request.user, INVITATIONS_MANAGE)

    def get(self, request, organization_id):
        from skeleton.tenancy import links

        return Response({"results": [link_data(item) for item in links.live_links()[:200]]})

    def post(self, request, organization_id):
        from django.urls import reverse

        from skeleton.access.models import Role
        from skeleton.tenancy import links

        data = request_fields(request)
        if set(data) != {"label", "role", "mode", "days", "max_uses"}:
            raise BoundaryError("invalid_request", "Informe label, role, mode, days e max_uses.")
        try:
            role = Role.objects.filter(pk=str(data["role"])).first()
        except (ValueError, ValidationError):
            role = None
        try:
            with transaction.atomic():
                link, token = links.create_join_link(
                    actor=request.user,
                    label=data["label"],
                    role=role,
                    mode=data["mode"],
                    days=data["days"],
                    max_uses=data["max_uses"],
                )
        except PermissionDenied:
            raise BoundaryError("privileged_role", "Só o administrador concede papel com capacidades.", 403) from None
        path = reverse("web:join-link", kwargs={"organization_id": organization_id, "token": token})
        response = Response({**link_data(link), "url": request.build_absolute_uri(path)}, status=201)
        response["Cache-Control"] = "no-store"
        return etag(response, link.revision)


class OrganizationJoinLinkRevokeAPI(OrganizationJoinLinksAPI):
    openapi = {
        "POST": Operation(
            "Revoga um link de ingresso", REVOKED_JOIN_LINK, if_match=True, capability="invitations.manage"
        )
    }

    def get(self, request, organization_id, sysid):
        raise Http404

    def post(self, request, organization_id, sysid):
        from skeleton.tenancy import links

        revision = _revision(request)
        try:
            with transaction.atomic():
                link = links.revoke_join_link(actor=request.user, link_id=sysid, expected_revision=revision)
        except links.InvalidJoinLink:
            raise Http404 from None
        except ConcurrentUpdate:
            raise _stale() from None
        return Response({"sysid": str(link.pk), "revoked": True})
