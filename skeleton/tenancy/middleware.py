from django.http import JsonResponse
from django.urls import resolve, Resolver404
from django.db import transaction
from django.core.exceptions import PermissionDenied

from .context import request_context, tenant_context, InvalidContext, current_context


class TenantContextMiddleware:
    """Own the entire handler and lazy template render transaction."""
    def __init__(self, get_response):
        self.get_response = get_response
        if getattr(get_response, "is_async", False):
            raise InvalidContext("Tenant middleware requires synchronous views.")

    def __call__(self, request):
        user = request.user
        with request_context(actor=user if user.is_authenticated else None,
                             request_id=request.request_id, source="http"):
            try:
                match = resolve(request.path_info)
            except Resolver404:
                match = None
            if match:
                from asgiref.sync import iscoroutinefunction
                if iscoroutinefunction(match.func):
                    raise InvalidContext("Async views cannot use transactional tenant middleware.")
            organization_id = match.kwargs.get("organization_id") if match and request.path_info.startswith(("/o/", "/api/v1/o/")) else None
            try:
                if organization_id and user.is_authenticated:
                    with tenant_context(organization_id=organization_id,
                                        actor=user if user.is_authenticated else None, source="http"):
                        response = self._handle(request)
                else:
                    response = self._handle(request)
            except PermissionDenied:
                if organization_id:
                    from django.http import HttpResponseNotFound
                    response = JsonResponse({"code": "not_found", "detail": "Not found."}, status=404) if request.path.startswith("/api/") else HttpResponseNotFound("Not found.")
                else:
                    raise
            request._skeleton_log_context = getattr(request, "_skeleton_log_context", current_context())
            if response.status_code >= 400:
                transaction.set_rollback(True)
            response["X-Request-ID"] = str(request.request_id)
            return response

    def _handle(self, request):
        response = self.get_response(request)
        if response.streaming:
            raise InvalidContext("Streaming responses are not supported inside database contexts.")
        if hasattr(response, "render") and not response.is_rendered:
            response.render()
        request._skeleton_log_context = current_context()
        return response

