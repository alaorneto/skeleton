"""Routes of the OIDC provider (#603): allauth's views behind the core's policy, and the devices page.

The protocol stays in ``allauth.idp.oidc.views``; each wrapper here only refuses before it or
completes after it:

- every route answers 404 while the provider has no signing key;
- ``authorize``: only an enabled native client, PKCE ``S256`` with a well-formed challenge and,
  when ``SKELETON_REQUIRE_ADMIN_MFA`` applies to the person, a configured TOTP;
- ``token``: only the ``authorization_code`` and ``refresh_token`` grants of an enabled native
  client; a renewal whose refresh token lost its person (``auth_version``, deactivation) is
  refused and its grant deleted; the issued tokens receive the ``skeleton`` stamp in the same
  transaction, so an unstamped token never authenticates;
- ``revoke``: revoking one token of a grant deletes the whole grant;
- ``userinfo``: the same stamp check as the API;
- the device flow does not exist (404).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from . import clients

logger = logging.getLogger("skeleton.security")
CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")


def _oauth_error(error: str, description: str, status: int = 400, **headers: str) -> JsonResponse:
    response = JsonResponse({"error": error, "error_description": description}, status=status)
    response["Cache-Control"] = "no-store"
    for name, value in headers.items():
        response[name] = value
    return response


def _enabled() -> None:
    if not clients.provider_enabled():
        raise Http404


def _needs_mfa(user: Any) -> bool:
    """The platform's administrative MFA policy, also for a native client sign-in."""
    from skeleton.accounts.mfa import mfa_required
    from skeleton.accounts.platform import platform_capabilities

    return bool(platform_capabilities(user)) and mfa_required(user)


def _authorization_refused(request: Any, native: Any, description: str) -> HttpResponse:
    """RFC 6749 §4.1.2.1: back to a registered redirect with ``invalid_request``, else a plain 400."""
    redirect_uri = request.GET.get("redirect_uri", "")
    if native is not None and clients.redirect_uri_allowed(redirect_uri, list(native.redirect_uris)):
        params = {"error": "invalid_request", "error_description": description}
        if request.GET.get("state"):
            params["state"] = request.GET["state"]
        response = HttpResponse(status=302)
        response["Location"] = f"{redirect_uri}?{urlencode(params)}"
        return response
    return HttpResponseBadRequest(description, content_type="text/plain; charset=utf-8")


@csrf_exempt
@never_cache
def authorize(request):
    from allauth.idp.oidc import views as idp

    _enabled()
    if request.method == "GET":
        native = clients.active_client(request.GET.get("client_id"))
        if native is None:
            return _authorization_refused(request, None, gettext("Cliente desconhecido ou desativado."))
        if not clients.redirect_uri_allowed(request.GET.get("redirect_uri"), list(native.redirect_uris)):
            return _authorization_refused(request, None, gettext("Redirect não registrado para este cliente."))
        if request.GET.get("code_challenge_method") != "S256" or not CHALLENGE.fullmatch(
            request.GET.get("code_challenge", "")
        ):
            return _authorization_refused(request, native, gettext("PKCE S256 obrigatório."))
        if request.GET.get("response_type") != "code":
            return _authorization_refused(request, native, gettext("Use response_type=code."))
    if request.user.is_authenticated and _needs_mfa(request.user):
        return redirect("mfa_index")
    return idp.authorization(request)


@csrf_exempt
@require_POST
def token(request):
    from allauth.idp.oidc import views as idp
    from allauth.idp.oidc.models import Token

    _enabled()
    grant_type = request.POST.get("grant_type")
    if grant_type not in clients.GRANT_TYPES:
        return _oauth_error("unsupported_grant_type", "Use authorization_code ou refresh_token.")
    client_id = request.POST.get("client_id")
    if clients.active_client(client_id) is None:
        return _oauth_error("invalid_client", "Cliente desconhecido ou desativado.", 401)
    previous = None
    with transaction.atomic():
        if grant_type == "refresh_token":
            current = clients.lookup(
                request.POST.get("refresh_token"), types=(Token.Type.REFRESH_TOKEN,), client_id=client_id
            )
            if current is not None and not clients.usable(current):
                clients.revoke_grant(current)
                logger.info("oidc_grant_invalidated", extra={"event": "oidc_grant_invalidated"})
                return _oauth_error("invalid_grant", "Sessão do dispositivo encerrada.")
            previous = clients.meta(current) if current is not None else None
        response = idp.token(request)
        if response.status_code == 200:
            body = json.loads(response.content)
            clients.stamp_issued(
                access_token=body["access_token"], refresh_token=body.get("refresh_token"), previous=previous
            )
    return response


@csrf_exempt
@require_POST
def revoke(request):
    from allauth.idp.oidc import views as idp
    from allauth.idp.oidc.models import Token

    _enabled()
    with transaction.atomic():
        current = clients.lookup(
            request.POST.get("token"),
            types=(Token.Type.ACCESS_TOKEN, Token.Type.REFRESH_TOKEN),
            client_id=request.POST.get("client_id") or None,
        )
        response = idp.revoke(request)
        if response.status_code == 200 and current is not None:
            clients.revoke_grant(current)
    return response


@csrf_exempt
def userinfo(request):
    from allauth.idp.oidc import views as idp
    from allauth.idp.oidc.models import Token

    _enabled()
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, value = header.partition(" ")
    current = clients.lookup(value.strip(), types=(Token.Type.ACCESS_TOKEN,)) if scheme.lower() == "bearer" else None
    if current is None or not clients.usable(current):
        return _oauth_error(
            "invalid_token", "Token inválido.", 401, **{"WWW-Authenticate": 'Bearer error="invalid_token"'}
        )
    return idp.user_info(request)


def configuration(request):
    from allauth.idp.oidc import views as idp

    _enabled()
    return idp.configuration(request)


def jwks(request):
    from allauth.idp.oidc import views as idp

    _enabled()
    return idp.jwks(request)


def logout(request):
    from allauth.idp.oidc import views as idp

    _enabled()
    return idp.logout(request)


def not_offered(request):
    """The device authorization grant is not offered to native clients (#603)."""
    raise Http404


# --------------------------------------------------------------------------- connected devices


@login_required
@never_cache
@require_http_methods(["GET"])
def connected_devices(request):
    if request.user.account_type != "human":
        raise Http404
    devices = clients.connected_devices(request.user)
    for device in devices:
        device["revoke_url"] = reverse("connected-device-revoke", args=[device["grant"]])
    return render(request, "account/connected_devices.html", {"devices": devices})


@login_required
@never_cache
@require_POST
def connected_device_revoke(request, grant):
    from django.contrib import messages

    with transaction.atomic():
        deleted = clients.revoke_device(request.user, grant)
    if deleted:
        logger.info("oidc_device_revoked", extra={"event": "oidc_device_revoked", "target_sysid": str(request.user.pk)})
        messages.success(request, gettext("Dispositivo desconectado."))
    return redirect("connected-devices")
