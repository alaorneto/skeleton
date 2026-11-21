from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import JsonResponse
from django.urls import Resolver404, resolve

from .context import InvalidContext, current_context, external_context, request_context, tenant_context

PORTAL_PREFIXES = ("/portal/", "/api/v1/portal/")


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
        # The GUC skeleton.source names the credential (token prefix or native client), so audit rows
        # show which one wrote.
        source = token.audit_source if token is not None else "http"
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
            portal_org = match.kwargs.get("organization_id") if match else None
            if portal_org and request.path_info.startswith(
                tuple(f"{prefix}{portal_org}/" for prefix in PORTAL_PREFIXES)
            ):
                # The portal of external people (#533): never a member context, never a token.
                return self._portal(request, portal_org, token)
            organization_id = (
                match.kwargs.get("organization_id")
                if match and request.path_info.startswith(("/o/", "/api/v1/o/"))
                else None
            )
            from .routing import site_organization_id

            site = site_organization_id()
            try:
                if site is not None and organization_id is not None and str(organization_id) != str(site):
                    # Defense in depth (#482): the host middleware already refused another organization.
                    raise PermissionDenied
                if organization_id and user.is_authenticated:
                    with tenant_context(
                        organization_id=organization_id, actor=user if user.is_authenticated else None, source=source
                    ):
                        # The membership is validated: a non-member still gets the 404 below.
                        response = limited_access_response(request, organization_id, user) or self._handle(request)
                else:
                    response = self._handle(request)
            except PermissionDenied:
                if organization_id:
                    from django.http import HttpResponseNotFound

                    if request.path.startswith("/api/"):
                        response = JsonResponse({"code": "not_found", "detail": "Not found."}, status=404)
                    elif site is not None and user.is_authenticated:
                        response = site_no_access_response(request, site)
                    else:
                        response = HttpResponseNotFound("Not found.")
                else:
                    raise
            request._skeleton_log_context = getattr(request, "_skeleton_log_context", current_context())
            if response.status_code >= 400:
                transaction.set_rollback(True)
            response["X-Request-ID"] = str(request.request_id)
            return response

    def _portal(self, request, organization_id, token):
        """Open ``external_context`` for ``/portal/<org>/`` and ``/api/v1/portal/<org>/``; any refusal is a 404."""
        from django.http import HttpResponseNotFound

        user = request.user
        try:
            if token is not None or not user.is_authenticated:
                raise PermissionDenied
            with external_context(organization_id=organization_id, actor=user, source="http:portal"):
                response = portal_access_response(request, organization_id) or self._handle(request)
                request._skeleton_log_context = current_context()
        except PermissionDenied:
            if not user.is_authenticated and not request.path.startswith("/api/"):
                from django.contrib.auth.views import redirect_to_login

                response = redirect_to_login(request.get_full_path())
            elif request.path.startswith("/api/"):
                response = JsonResponse({"code": "not_found", "detail": "Not found."}, status=404)
            else:
                response = HttpResponseNotFound("Not found.")
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


def site_no_access_response(request, organization_id):
    """The organization host for a signed-in person without membership (#483): the same page for any path.

    It never says whether the requested record exists, and shows only the public site name.
    """
    from django.shortcuts import render

    from .admission import JoinUnavailable, accepting

    try:
        accepting(organization_id)
    except JoinUnavailable:
        accepts = False
    else:
        accepts = True
    context = {"accepts_requests": accepts, "organization_id": organization_id}
    return render(request, "skeleton/tenancy/site_no_access.html", context, status=404)


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _segment(path):
    """The first path segment after ``/o/<id>/`` or ``/api/v1/o/<id>/``."""
    parts = path.split("/")
    index = 5 if path.startswith("/api/v1/o/") else 3
    return parts[index] if len(parts) > index else ""


def limited_access_response(request, organization_id, user):
    """The answer for an organization whose billing standing limits access, or ``None`` (#395).

    Restricted: people read but do not write. Suspended: only the exempt routes (billing),
    for who may settle the account. Both stay reachable for that person while limited.
    """
    from skeleton.common.entitlements import (
        AccessLevel,
        exempt_segments,
        limited_code,
        may_use_exempt_routes,
        organization_access,
        writes_allowed,
    )

    level = organization_access(organization_id)
    request.skeleton_access_level = level
    if writes_allowed(level):
        return None
    if _segment(request.path_info) in exempt_segments() and may_use_exempt_routes(organization_id, user):
        return None
    if level is AccessLevel.RESTRICTED and request.method in SAFE_METHODS:
        return None
    return _limited_response(request, limited_code(level))


def portal_access_response(request, organization_id):
    """The portal under a limited billing standing (#533): suspended closes it, restricted only reads."""
    from django.shortcuts import render
    from django.utils.translation import gettext

    from skeleton.common.entitlements import AccessLevel, organization_access, writes_allowed

    level = organization_access(organization_id)
    request.skeleton_access_level = level
    if writes_allowed(level):
        return None
    if level is AccessLevel.RESTRICTED and request.method in SAFE_METHODS:
        return None
    code = "portal_unavailable" if level is AccessLevel.SUSPENDED else "portal_read_only"
    detail = (
        gettext("Este serviço está indisponível no momento.")
        if level is AccessLevel.SUSPENDED
        else gettext("Este serviço está disponível só para consulta no momento.")
    )
    status = 503 if level is AccessLevel.SUSPENDED else 403
    if request.path.startswith("/api/"):
        return JsonResponse({"code": code, "detail": detail}, status=status)
    return render(request, "skeleton/portal/unavailable.html", {"detail": detail}, status=status)


def _limited_response(request, code):
    from django.shortcuts import render
    from django.utils.translation import gettext

    if code == "organization_suspended":
        detail = gettext(
            "O acesso a esta organização está suspenso por pendência de pagamento. "
            "Quem administra a cobrança da organização pode regularizá-la."
        )
    else:
        detail = gettext(
            "Esta organização está em modo somente leitura por pendência de pagamento. "
            "Quem administra a cobrança da organização pode regularizá-la."
        )
    if request.path.startswith("/api/"):
        return JsonResponse({"code": code, "detail": detail}, status=403)
    return render(request, "skeleton/tenancy/organization_access.html", {"detail": detail, "code": code}, status=403)
