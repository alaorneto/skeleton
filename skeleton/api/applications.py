"""REST administration of the organization's apps (/api/v1/o/<org>/apps/), over the service API (#338)."""

import re

from django.http import Http404
from django.utils.translation import gettext
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError
from skeleton.api.openapi import (
    BOOLEAN,
    INTEGER,
    OBJECT,
    ORGANIZATION_ADMIN,
    UUID,
    Operation,
    nullable,
    obj,
    results,
    string,
)
from skeleton.api.views import TenantAPI, etag, request_fields
from skeleton.system.administration import require_apps_manager
from skeleton.system.app_registry import get_app


def _manifest(app_label):
    try:
        return get_app(app_label)
    except KeyError:
        raise Http404 from None


def state_data(state):
    """REST shape of an ApplicationState (unchanged from the former application_state dict)."""
    return {
        "app_label": state.app_label,
        "available": state.available,
        "label": state.label,
        "description": state.description,
        "app_id": state.app_id,
        "version": state.version,
        "enabled": state.enabled,
        "configuration": {item.name: item.value for item in state.configuration},
        "revision": state.revision,
        "sysid": state.sysid,
    }


APPLICATION_STATE = obj(
    {
        "app_label": string(),
        "available": BOOLEAN,
        "label": string(),
        "description": string(),
        "app_id": string(),
        "version": string(),
        "enabled": BOOLEAN,
        "configuration": OBJECT,
        "revision": INTEGER,
        "sysid": nullable(UUID),
    }
)


class ApplicationAPI(TenantAPI):
    openapi_detail = "app_label"
    openapi = {
        "GET": (
            Operation("Apps da organização", results(APPLICATION_STATE), detail=False, capability=ORGANIZATION_ADMIN),
            Operation(
                "Lê o estado de uma app", APPLICATION_STATE, etag=True, detail=True, capability=ORGANIZATION_ADMIN
            ),
        ),
        "PUT": Operation(
            "Ativa, desativa ou configura uma app",
            APPLICATION_STATE,
            etag=True,
            if_match=True,
            detail=True,
            request=obj({"enabled": BOOLEAN, "configuration": OBJECT}),
            capability=ORGANIZATION_ADMIN,
        ),
    }

    def check_permissions(self, request):
        super().check_permissions(request)
        require_apps_manager(request.user)

    def get(self, request, organization_id, app_label=None):
        from skeleton.service_api.applications import ApplicationAdministrationService

        service = ApplicationAdministrationService()
        if app_label is None:
            return Response({"results": [state_data(item) for item in service.applications(actor=request.user)]})
        state = service.application(actor=request.user, app_label=app_label)
        return etag(Response(state_data(state)), state.revision)

    def put(self, request, organization_id, app_label):
        _manifest(app_label)
        value = request.headers.get("If-Match")
        if value is None:
            raise BoundaryError("revision_required", gettext("Informe a revisão."), 428)
        if not re.fullmatch(r'"(?:0|[1-9][0-9]{0,18})"', value):
            raise BoundaryError("invalid_revision", gettext("Revisão inválida."))
        data = request_fields(request)
        if set(data) != {"enabled", "configuration"}:
            raise BoundaryError("invalid_request", gettext("Informe enabled e configuration."))
        from skeleton.api.errors import if_match_precondition
        from skeleton.service_api.applications import ApplicationAdministrationService

        service = ApplicationAdministrationService()
        # The service lists available apps and enabled ones the platform withdrew (#24).
        state = next((item for item in service.applications(actor=request.user) if item.app_label == app_label), None)
        if state is None or (not state.available and data["enabled"] is not False):
            # Only closing an app the platform withdrew is accepted; the rest is unavailable.
            raise BoundaryError("app_unavailable", "Aplicativo indisponível para esta organização.", 400)
        configuration = data["configuration"]
        with if_match_precondition():
            state = service.configure(
                actor=request.user,
                app_label=app_label,
                enabled=data["enabled"],
                configuration=tuple(configuration.items()) if isinstance(configuration, dict) else configuration,
                expected_revision=int(value[1:-1]),
            )
        return etag(Response(state_data(state)), state.revision)
