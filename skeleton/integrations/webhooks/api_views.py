"""REST of the organization's webhooks under ``/api/v1/o/<org>/webhooks/`` (#63).

``integrations.manage`` only (404 otherwise). Reads never carry a secret; ``POST`` (create)
and ``rotate`` return it once, with ``Cache-Control: no-store``, and need a recent
authentication. ``PATCH``, ``DELETE`` and ``rotate`` require ``If-Match`` with the revision.
"""

from django.db import transaction
from django.http import Http404
from django.views.decorators.cache import add_never_cache_headers
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError, parse_revision
from skeleton.api.views import TenantAPI, etag, page, request_fields

from . import services


def endpoint_data(endpoint):
    return {
        "sysid": endpoint.sysid,
        "name": endpoint.name,
        "url": endpoint.url,
        "principal": endpoint.principal_id,
        "event_types": list(endpoint.event_types),
        "is_enabled": endpoint.is_enabled,
        "disabled_at": endpoint.disabled_at,
        "disabled_reason": endpoint.disabled_reason,
        "previous_secret_valid_until": endpoint.previous_valid_until,
        "revision": endpoint.revision,
        "created_at": endpoint.created_at,
        "updated_at": endpoint.updated_at,
    }


def _with_secret(issued, status):
    response = Response({**endpoint_data(issued.endpoint), "secret": issued.secret}, status=status)
    add_never_cache_headers(response)
    return etag(response, issued.endpoint.revision)


class WebhookAPI(TenantAPI):
    def check_permissions(self, request):
        super().check_permissions(request)
        from skeleton.access.capabilities import INTEGRATIONS_MANAGE, has_capability

        if not has_capability(request.user, INTEGRATIONS_MANAGE):
            raise Http404

    def recent_authentication(self, request):
        from skeleton.accounts.reauthentication import has_recent_authentication

        if not has_recent_authentication(request._request):
            raise BoundaryError("reauthentication_required", "Autentique-se de novo para ver um segredo.", 403)

    def endpoint(self, sysid):
        from .models import WebhookEndpoint

        endpoint = WebhookEndpoint.objects.filter(pk=sysid).first()
        if endpoint is None:
            raise Http404
        return endpoint


def _input(request):
    data = request_fields(request)
    if "principal" in data and not isinstance(data["principal"], str):
        raise BoundaryError("invalid_request", "principal é o UUID de uma conta de serviço.")
    if "principal" in data:
        from uuid import UUID

        try:
            data["principal"] = UUID(data["principal"])
        except ValueError:
            raise BoundaryError("invalid_request", "principal é o UUID de uma conta de serviço.") from None
    return data


class WebhooksView(WebhookAPI):
    def get(self, request, organization_id):
        from .models import WebhookEndpoint

        if set(request.query_params) - {"page", "page_size"}:
            raise BoundaryError("invalid_filter", "Filtro não permitido.")
        return page(WebhookEndpoint.objects.order_by("name", "sysid"), request, endpoint_data)

    @transaction.atomic
    def post(self, request, organization_id):
        self.recent_authentication(request)
        issued = services.create_endpoint(actor=request.user, data=_input(request))
        return _with_secret(issued, 201)


class WebhookDetailView(WebhookAPI):
    def get(self, request, organization_id, sysid):
        endpoint = self.endpoint(sysid)
        return etag(Response(endpoint_data(endpoint)), endpoint.revision)

    @transaction.atomic
    def patch(self, request, organization_id, sysid):
        endpoint = self.endpoint(sysid)
        revision = parse_revision(request.headers.get("If-Match"))
        endpoint = services.update_endpoint(
            endpoint, actor=request.user, data=_input(request), expected_revision=revision
        )
        return etag(Response(endpoint_data(endpoint)), endpoint.revision)

    @transaction.atomic
    def delete(self, request, organization_id, sysid):
        endpoint = self.endpoint(sysid)
        if request_fields(request):
            raise BoundaryError("invalid_request", "Remoção não aceita campos.")
        services.delete_endpoint(
            endpoint, actor=request.user, expected_revision=parse_revision(request.headers.get("If-Match"))
        )
        return Response(status=204)


class WebhookRotateView(WebhookAPI):
    @transaction.atomic
    def post(self, request, organization_id, sysid):
        endpoint = self.endpoint(sysid)
        if request_fields(request):
            raise BoundaryError("invalid_request", "A rotação não aceita campos.")
        self.recent_authentication(request)
        issued = services.rotate_secret(
            endpoint, actor=request.user, expected_revision=parse_revision(request.headers.get("If-Match"))
        )
        return _with_secret(issued, 200)
