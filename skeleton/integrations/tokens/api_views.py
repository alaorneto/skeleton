"""REST of tokens and service accounts (#47), session only.

``/api/v1/me/tokens/`` lists and issues the actor's personal tokens;
``/api/v1/o/<org>/service-accounts/`` and its ``tokens/`` need ``integrations.manage`` (404
otherwise). None of these views declares ``token_methods``: a request authenticated by a
token gets 403 ``token_not_allowed`` (a token never issues or revokes tokens). Issuing needs a
recent authentication and returns the token text once, with ``Cache-Control: no-store``;
revocation needs ``If-Match``.
"""

from __future__ import annotations

from datetime import datetime

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import add_never_cache_headers
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError, parse_revision
from skeleton.api.views import SessionAPI, TenantAPI, etag, request_fields
from skeleton.common.exceptions import ConcurrentUpdate

from . import services
from .models import ApiToken


def token_data(token):
    return {
        "sysid": token.sysid,
        "name": token.name,
        "kind": token.kind,
        "prefix": token.prefix,
        "scopes": list(token.scopes),
        "expires_at": token.expires_at,
        "revoked_at": token.revoked_at,
        "organization": token.bound_organization_id,
        "revision": token.revision,
        "created_at": token.created_at,
    }


def account_data(user):
    return {"sysid": user.sysid, "name": user.first_name, "email": user.email, "is_active": user.is_active}


def _issued(issued):
    response = Response({**token_data(issued.token), "token": issued.text}, status=201)
    add_never_cache_headers(response)
    return etag(response, issued.token.revision)


def _issue_fields(request):
    fields = request_fields(request)
    if set(fields) - {"name", "scopes", "expires_at"}:
        raise BoundaryError("invalid_request", "Campos aceitos: name, scopes, expires_at.")
    expires = fields.get("expires_at")
    parsed = parse_datetime(expires) if isinstance(expires, str) else None
    return fields.get("name"), fields.get("scopes"), parsed if isinstance(parsed, datetime) else expires


def _require_recent(request):
    from skeleton.accounts.reauthentication import has_recent_authentication

    if not has_recent_authentication(request._request):
        raise BoundaryError("reauthentication_required", "Autentique-se de novo para emitir um token.", 403)


def _revoke(request, token_id):
    revision = parse_revision(request.headers.get("If-Match"))
    try:
        with transaction.atomic():
            services.revoke_token(actor=request.user, token_id=token_id, expected_revision=revision)
    except ApiToken.DoesNotExist:
        raise Http404 from None
    except ConcurrentUpdate:
        raise BoundaryError("revision_conflict", "O token mudou.", 412) from None
    return Response(status=204)


class MeTokensView(SessionAPI):
    def get(self, request):
        if request.user.account_type != "human":
            raise Http404
        return Response({"results": [token_data(token) for token in services.personal_tokens(request.user)]})

    @transaction.atomic
    def post(self, request):
        if request.user.account_type != "human":
            raise Http404
        _require_recent(request)
        name, scopes, expires_at = _issue_fields(request)
        try:
            issued = services.issue_personal_token(actor=request.user, name=name, scopes=scopes, expires_at=expires_at)
        except PermissionDenied:
            raise BoundaryError("mfa_required", "Configure autenticação multifator para continuar.", 403) from None
        return _issued(issued)


class MeTokenDetailView(SessionAPI):
    @transaction.atomic
    def delete(self, request, sysid):
        return _revoke(request, sysid)


class ServiceAccountAPI(TenantAPI):
    def check_permissions(self, request):
        super().check_permissions(request)
        from skeleton.access.capabilities import INTEGRATIONS_MANAGE, has_capability

        if not has_capability(request.user, INTEGRATIONS_MANAGE):
            raise Http404

    def accounts(self, organization_id):
        from skeleton.accounts.models import AccountType, User

        return User.objects.filter(account_type=AccountType.SERVICE, service_organization_id=organization_id).order_by(
            "first_name", "sysid"
        )

    def account(self, organization_id, sysid):
        account = self.accounts(organization_id).filter(pk=sysid).first()
        if account is None:
            raise Http404
        return account


class ServiceAccountsView(ServiceAccountAPI):
    def get(self, request, organization_id):
        return Response({"results": [account_data(user) for user in self.accounts(organization_id)]})

    @transaction.atomic
    def post(self, request, organization_id):
        from skeleton.accounts.services import create_service_account

        fields = request_fields(request)
        if set(fields) != {"name"}:
            raise BoundaryError("invalid_request", "Envie só name.")
        return Response(account_data(create_service_account(actor=request.user, name=fields["name"])), status=201)


class ServiceAccountDetailView(ServiceAccountAPI):
    @transaction.atomic
    def delete(self, request, organization_id, sysid):
        from skeleton.accounts.services import deactivate_service_account

        if request_fields(request):
            raise BoundaryError("invalid_request", "Desativação não aceita campos.")
        deactivate_service_account(actor=request.user, user=self.account(organization_id, sysid))
        return Response(status=204)


class ServiceAccountTokensView(ServiceAccountAPI):
    def get(self, request, organization_id, sysid):
        account = self.account(organization_id, sysid)
        rows = services.service_tokens(organization_id, account=account)
        return Response({"results": [token_data(token) for token in rows]})

    @transaction.atomic
    def post(self, request, organization_id, sysid):
        account = self.account(organization_id, sysid)
        _require_recent(request)
        name, scopes, expires_at = _issue_fields(request)
        try:
            issued = services.issue_service_token(
                actor=request.user, account=account, name=name, scopes=scopes, expires_at=expires_at
            )
        except PermissionDenied:
            raise BoundaryError("mfa_required", "Configure autenticação multifator para continuar.", 403) from None
        return _issued(issued)


class ServiceAccountTokenDetailView(ServiceAccountAPI):
    @transaction.atomic
    def delete(self, request, organization_id, sysid, token_id):
        account = self.account(organization_id, sysid)
        if not services.service_tokens(organization_id, account=account).filter(pk=token_id).exists():
            raise Http404
        return _revoke(request, token_id)


__all__ = [
    "MeTokenDetailView",
    "MeTokensView",
    "ServiceAccountDetailView",
    "ServiceAccountTokenDetailView",
    "ServiceAccountTokensView",
    "ServiceAccountsView",
]
