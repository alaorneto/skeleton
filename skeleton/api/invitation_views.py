from django.db import transaction
from django.http import Http404
from django.utils.translation import gettext
from rest_framework.response import Response

from skeleton.access.capabilities import INVITATIONS_MANAGE, require_resource_capability
from skeleton.api.errors import BoundaryError, parse_revision
from skeleton.api.openapi import Operation, ref
from skeleton.api.serializers import dedicated_data
from skeleton.api.views import TenantAPI, etag, request_fields
from skeleton.tenancy.invitations import resend_invitation
from skeleton.tenancy.models import Invitation


class ResendInvitationView(TenantAPI):
    openapi = {
        "POST": Operation(
            "Reenvia o convite (um convite novo substitui o atual)",
            ref("Dedicated.invitations"),
            status=201,
            etag=True,
            if_match=True,
            capability=INVITATIONS_MANAGE,
        )
    }

    @transaction.atomic
    def post(self, request, organization_id, sysid):
        require_resource_capability(request.user, INVITATIONS_MANAGE)
        if request_fields(request):
            raise BoundaryError("invalid_request", gettext("Reenvio não aceita campos."))
        invitation = Invitation.objects.filter(pk=sysid).first()
        if invitation is None:
            raise Http404
        # The service locks the row and compares the revision (#340): the view only reads If-Match.
        revision = parse_revision(request.headers.get("If-Match"))
        created = resend_invitation(invitation, user=request.user, revision=revision, request=request)
        return etag(Response(dedicated_data("invitations", created), status=201), created.revision)
