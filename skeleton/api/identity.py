"""REST of the organization's SSO admission rules (#31)."""

from __future__ import annotations

import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from rest_framework.response import Response

from skeleton.access.capabilities import IDENTITY_CONFIGURE, require_resource_capability
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy import identity_admission

from .errors import BoundaryError
from .views import TenantAPI, etag, request_fields


def provider_data(item):
    row = item.admission
    return {
        "sysid": str(item.sysid),
        "provider_id": item.provider_id,
        "name": item.name,
        "protocol": item.protocol,
        "enabled": item.enabled,
        "show_on_login": item.show_on_login,
        "login_path": item.login_path,
        "last_check_at": item.last_check_at.isoformat() if item.last_check_at else None,
        "last_check_code": item.last_check_code,
        "admission": {
            "jit_provisioning": bool(row and row.jit_provisioning),
            "jit_requires_approval": bool(row and row.jit_requires_approval),
            "default_role": str(row.default_role_id) if row and row.default_role_id else None,
            "allowed_email_domains": list(row.allowed_email_domains) if row else [],
            "revision": item.revision,
        },
    }


class OrganizationIdentityProvidersAPI(TenantAPI):
    def check_permissions(self, request):
        super().check_permissions(request)
        require_resource_capability(request.user, IDENTITY_CONFIGURE)

    def get(self, request, organization_id):
        return Response({"results": [provider_data(item) for item in identity_admission.providers()]})


class OrganizationIdentityProviderAdmissionAPI(OrganizationIdentityProvidersAPI):
    def get(self, request, organization_id, sysid):
        try:
            item = identity_admission.provider(sysid)
        except identity_admission.ProviderNotFound:
            raise Http404 from None
        return etag(Response(provider_data(item)["admission"]), item.revision)

    def put(self, request, organization_id, sysid):
        value = request.headers.get("If-Match")
        if value is None:
            raise BoundaryError("revision_required", "Informe a revisão.", 428)
        if not re.fullmatch(r'"(?:0|[1-9][0-9]{0,18})"', value):
            raise BoundaryError("invalid_revision", "Revisão inválida.")
        data = request_fields(request)
        if set(data) != set(identity_admission.FIELDS):
            raise BoundaryError("invalid_request", "Informe " + ", ".join(identity_admission.FIELDS) + ".")
        try:
            with transaction.atomic():
                identity_admission.configure(
                    actor=request.user, provider_sysid=sysid, data=data, expected_revision=int(value[1:-1])
                )
        except identity_admission.ProviderNotFound:
            raise Http404 from None
        except ConcurrentUpdate:
            raise BoundaryError("revision_conflict", "As regras foram alteradas por outra operação.", 412) from None
        except PermissionDenied:
            raise BoundaryError("privileged_role", "Só o administrador concede papel com capacidades.", 403) from None
        except ValidationError:
            raise
        item = identity_admission.provider(sysid)
        return etag(Response(provider_data(item)["admission"]), item.revision)
