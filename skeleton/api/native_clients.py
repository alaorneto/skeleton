"""``GET /api/v1/clients/<client_id>/policy`` (#606): the update policy of a native client, anonymous.

The client sends its ``version``, ``os`` and ``channel`` and learns the newest live version of
the channel, the minimum one the operator requires and whether it must update. Anonymous by
design (a client too old to sign in still asks); nothing but the published versions comes out.
"""

from __future__ import annotations

from django.http import Http404
from django.utils.translation import gettext
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError
from skeleton.api.openapi import BOOLEAN, Operation, nullable, obj, string
from skeleton.api.views import SessionAPI

POLICY = obj({"latest_version": nullable(string()), "minimum_version": nullable(string()), "update_required": BOOLEAN})


class NativeClientPolicyView(SessionAPI):
    permission_classes = [AllowAny]
    token_methods = ("GET",)
    openapi = {
        "GET": Operation(
            "Política de atualização de um cliente nativo (anônima)",
            POLICY,
            query=(
                ("version", string(), "Versão instalada (X.Y.Z)."),
                ("os", string(), "Sistema do feed, como win."),
                ("channel", string(), "Canal, como stable."),
            ),
        )
    }

    def get(self, request, client_id):
        from skeleton.system.native_clients.config import CLIENT_ID, NAME
        from skeleton.system.native_clients.services import policy

        params = request.query_params
        if set(params) - {"version", "os", "channel"} or not all(
            params.get(key) for key in ("version", "os", "channel")
        ):
            raise BoundaryError("invalid_filter", gettext("Informe version, os e channel."))
        if (
            not CLIENT_ID.fullmatch(client_id)
            or not NAME.fullmatch(params["os"])
            or not NAME.fullmatch(params["channel"])
        ):
            raise Http404
        try:
            data = policy(client_id=client_id, channel=params["channel"], os=params["os"], version=params["version"])
        except ValueError:
            raise BoundaryError("invalid_filter", gettext("Versão inválida.")) from None
        if data is None:
            raise Http404
        response = Response(data)
        response["Cache-Control"] = "public, max-age=60"
        return response
