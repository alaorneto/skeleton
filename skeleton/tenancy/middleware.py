from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import JsonResponse
from django.urls import Resolver404, resolve

from .context import InvalidContext, current_context, request_context, tenant_context


class TenantContextMiddleware:
    """Own the entire handler and lazy template render transaction."""

    def __init__(self, get_response):
        self.get_response = get_response
        if getattr(get_response, "is_async", False):
            raise InvalidContext("Tenant middleware requires synchronous views.")

    def __call__(self, request):
        from .aggregate import is_aggregate_route

        try:
            match = resolve(request.path_info)  # Resolved once per request (#237).
        except Resolver404:
            match = None
        if is_aggregate_route(match.func if match else None):
            # Reads many organizations (#74): the view opens one context per organization itself.
            response = self.get_response(request)
            if hasattr(response, "render") and not response.is_rendered:
                response.render()
            response["X-Request-ID"] = str(request.request_id)
            return response
        response = self._in_context(request, match)
        if getattr(response, "detached", False):
            # The transaction committed and the contexts are gone: stream without the ORM.
            response = response.materialize()
        return response

    def _in_context(self, request, match):
        user = request.user
        token = getattr(request, "api_token", None)
        # The GUC skeleton.source names the token prefix, so audit rows show which credential wrote.
        source = f"http:token:{token.prefix}" if token is not None else "http"
        with request_context(
            actor=user if user.is_authenticated else None,
            request_id=request.request_id,
            source=source,
            credential_scopes=frozenset(token.scopes) if token is not None else None,
        ):
            if match:
                from asgiref.sync import iscoroutinefunction

                if iscoroutinefunction(match.func):
                    raise InvalidContext("Async views cannot use transactional tenant middleware.")
            organization_id = (
                match.kwargs.get("organization_id")
                if match and request.path_info.startswith(("/o/", "/api/v1/o/"))
                else None
            )
            try:
                if organization_id and user.is_authenticated:
                    with tenant_context(
                        organization_id=organization_id, actor=user if user.is_authenticated else None, source=source
                    ):
                        response = self._handle(request)
                else:
                    response = self._handle(request)
            except PermissionDenied:
                if organization_id:
                    from django.http import HttpResponseNotFound

                    response = (
                        JsonResponse({"code": "not_found", "detail": "Not found."}, status=404)
                        if request.path.startswith("/api/")
                        else HttpResponseNotFound("Not found.")
                    )
                else:
                    raise
            request._skeleton_log_context = getattr(request, "_skeleton_log_context", current_context())
            if response.status_code >= 400:
                transaction.set_rollback(True)
            response["X-Request-ID"] = str(request.request_id)
            return response

    def process_exception(self, request, exception):
        """An app withdrawn by the platform looks like a missing route, not a forbidden one."""
        from django.http import HttpResponseNotFound

        from skeleton.common.exceptions import ApplicationUnavailable

        if not isinstance(exception, ApplicationUnavailable):
            return None
        if request.path.startswith("/api/"):
            return JsonResponse({"code": "not_found", "detail": "Not found."}, status=404)
        return HttpResponseNotFound("Not found.")

    def _handle(self, request):
        response = self.get_response(request)
        if response.streaming:
            raise InvalidContext("Streaming responses are not supported inside database contexts.")
        if hasattr(response, "render") and not response.is_rendered:
            response.render()
        request._skeleton_log_context = current_context()
        if response.status_code >= 400:
            # Revert the tenant savepoint inside its own context before leaving it (#237).
            transaction.set_rollback(True)
        return response
