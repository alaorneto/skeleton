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
from .views import SessionAPI, TenantAPI, etag, request_fields


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
