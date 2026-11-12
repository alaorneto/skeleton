"""Bearer tokens on ``/api/v1/`` (#46), between ``AuthVersionMiddleware`` and the tenant context.

Only ``/api/v1/`` reads the ``Authorization`` header; HTML routes ignore it. For a Bearer token:

1. a session in the same request answers 400 ``ambiguous_credentials``;
2. a malformed, unknown, expired or revoked token, or one whose user is inactive or changed
   ``auth_version``, answers 401 ``invalid_token`` with ``WWW-Authenticate``;
3. a view that does not list the method in ``token_methods`` answers 403 ``token_not_allowed``
   (access administration, app configuration, webhooks, audit, ``PATCH me``, token management);
4. a token bound to an organization used on another organization's URL answers 404, like a
   resource the user cannot see.

Rate limits (#48), in the shared cache: an IP with more than
``SKELETON_API_TOKEN_FAILURES_PER_MINUTE`` failed token authentications gets 429 before any
lookup (prefix brute force), and a token above ``SKELETON_API_TOKEN_RATE_PER_MINUTE`` requests
gets 429 before its view runs, so nothing is written. Both answers carry ``Retry-After``.

Otherwise the request runs as the token's user, without CSRF (a Bearer header is never sent by
the browser on its own), and the tenant middleware records ``http:token:<prefix>`` as source
and the token scopes in the context.
"""

from __future__ import annotations

from django.http import JsonResponse
from django.urls import Resolver404, resolve
from django.utils.translation import gettext

from skeleton.common.logging import security_event
from skeleton.common.ratelimit import hit, peek

API_PREFIX = "/api/v1/"
SAFE_EXTRA = ("HEAD", "OPTIONS")
WINDOW_S = 60


def _limit(name: str, default: int) -> int:
    from django.conf import settings

    return int(getattr(settings, name, default))


def client_ip(request) -> str:
    """The client address under the configured proxy topology (allauth's trusted proxy count)."""
    try:
        from allauth.account.adapter import get_adapter

        return str(get_adapter(request).get_client_ip(request))
    except Exception:  # noqa: BLE001 - a malformed forwarded header falls back to the peer
        return request.META.get("REMOTE_ADDR", "unknown")


def _too_many(request, reason: str, retry_after: int):
    security_event("api_token.rate_limited", reason_code=reason, source="http", request_id=request.request_id)
    return _error(
        429,
        "rate_limited",
        gettext("Muitas requisições. Tente de novo mais tarde."),
        **{"Retry-After": str(retry_after)},
    )


def _error(status: int, code: str, detail: str, **headers: str) -> JsonResponse:
    response = JsonResponse({"code": code, "detail": detail}, status=status)
    for name, value in headers.items():
        response[name] = value
    return response


def bearer(request) -> str | None:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip()


def _view_class(match):
    func = match.func
    return getattr(func, "view_class", None) or getattr(func, "cls", None)


def token_methods(match) -> tuple[str, ...]:
    methods = tuple(getattr(_view_class(match), "token_methods", ()) or ())
    return methods + (SAFE_EXTRA if "GET" in methods else ())


class ApiTokenMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.api_token = None
        if not request.path_info.startswith(API_PREFIX):
            return self.get_response(request)
        text = bearer(request)
        if text is None:
            return self.get_response(request)
        return self._authenticate(request, text) or self.get_response(request)

    def _reject(self, request, reason: str):
        hit(f"token-failure:{client_ip(request)}", limit=0, window_s=WINDOW_S)
        security_event("api_token.rejected", reason_code=reason, source="http", request_id=request.request_id)
        return _error(
            401,
            "invalid_token",
            gettext("Token inválido, expirado ou revogado."),
            **{"WWW-Authenticate": 'Bearer realm="api", error="invalid_token"'},
        )

    def _authenticate(self, request, text: str):
        from .services import verify_token

        if request.user.is_authenticated:
            security_event(
                "api_token.rejected", reason_code="ambiguous_credentials", source="http", request_id=request.request_id
            )
            return _error(400, "ambiguous_credentials", gettext("Use a sessão ou o token, não os dois."))
        failures = f"token-failure:{client_ip(request)}"
        if peek(failures, window_s=WINDOW_S) >= _limit("SKELETON_API_TOKEN_FAILURES_PER_MINUTE", 20):
            # Brute force of prefixes or secrets: refused before any database lookup.
            return _too_many(request, "failures", WINDOW_S)
        token = verify_token(text)
        if token is None or token.kind == token.Kind.FEED:
            # A feed token only opens the feed views that check its scope, never /api/v1/.
            return self._reject(request, "invalid_token")
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return None  # The normal 404, without running as the token user.
        if request.method not in token_methods(match):
            security_event(
                "api_token.rejected", reason_code="token_not_allowed", source="http", request_id=request.request_id
            )
            return _error(403, "token_not_allowed", gettext("Este recurso não aceita token de API."))
        organization_id = match.kwargs.get("organization_id")
        if token.bound_organization_id is not None and organization_id not in (None, token.bound_organization_id):
            return _error(404, "not_found", "Not found.")
        rate = hit(f"token:{token.pk}", limit=_limit("SKELETON_API_TOKEN_RATE_PER_MINUTE", 600), window_s=WINDOW_S)
        if not rate.allowed:
            return _too_many(request, "token", rate.retry_after)
        from .activity import touch

        touch(token)
        request.user = token.user
        request.api_token = token
        # No browser attaches a Bearer header by itself, so CSRF does not apply to this request.
        request._dont_enforce_csrf_checks = True
        return None
