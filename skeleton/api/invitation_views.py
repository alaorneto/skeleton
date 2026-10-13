from django.db import transaction
from django.http import Http404
from rest_framework.response import Response
from skeleton.access.services import require_organization_admin
from skeleton.api.errors import BoundaryError, require_revision
from skeleton.api.serializers import dedicated_data
from skeleton.api.views import TenantAPI, etag, request_fields
from skeleton.tenancy.invitations import resend_invitation
from skeleton.tenancy.models import Invitation


class ResendInvitationView(TenantAPI):
    @transaction.atomic
    def post(self, request, organization_id, sysid):
        require_organization_admin(request.user)
        if request_fields(request):
            raise BoundaryError('invalid_request', 'Reenvio não aceita campos.')
        invitation = Invitation.objects.select_for_update().filter(pk=sysid).first()
        if invitation is None:
            raise Http404
        require_revision(request, invitation)
        created = resend_invitation(invitation, user=request.user, revision=invitation.revision, request=request)
        return etag(Response(dedicated_data('invitations', created), status=201), created.revision)
