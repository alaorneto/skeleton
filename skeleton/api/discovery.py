"""Discovery for native clients (#605): the server before login and the active apps after it.

``GET /api/v1/server/`` is anonymous by design and answers only public facts of the product
(name, core version, API versions, OIDC issuer): no organization, host list, setting or secret.
It stays open in the administrative maintenance mode, so a client can tell "down for
maintenance" from "not a server of the core". ``GET /api/v1/o/<org>/apps/active/`` lists the
apps a member can use in the organization, with a session or a token; a non-member gets the
neutral 404 of the tenant middleware.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from skeleton.api.openapi import Operation, array, nullable, obj, results, string
from skeleton.api.views import SessionAPI, TenantAPI

API_VERSIONS = ("v1",)
SERVER_CACHE_SECONDS = 60

SERVER = obj(
    {
        "product": string(),
        "core_version": nullable(string()),
        "api_versions": array(string()),
        "oidc_issuer": nullable(string()),
    }
)
ACTIVE_APP = obj({"app_id": string(), "label": string(), "version": string(), "title": string()})


def server_data(request) -> dict:
    from skeleton.accounts.idp.clients import issuer
    from skeleton.versioning import core_version

    return {
        "product": str(getattr(settings, "SKELETON_PRODUCT_NAME", "") or "Skeleton"),
        "core_version": core_version()["version"],
        "api_versions": list(API_VERSIONS),
        "oidc_issuer": issuer(request),
    }


class ServerView(SessionAPI):
    permission_classes = [AllowAny]
    token_methods = ("GET",)
    openapi = {"GET": Operation("Informação pública do servidor (anônima)", SERVER)}

    def get(self, request):
        response = Response(server_data(request))
        response["Cache-Control"] = f"public, max-age={SERVER_CACHE_SECONDS}"
        return response


class ActiveApplicationsView(TenantAPI):
    token_methods = ("GET",)
    openapi = {"GET": Operation("Apps ativas da organização para o ator", results(ACTIVE_APP))}

    def get(self, request, organization_id):
        from skeleton.system.applications import active_applications_for

        return Response({"results": active_applications_for(request.user)})
