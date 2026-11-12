"""REST for attachments under the target record: list/upload, remove and restore with If-Match."""

from django.db import transaction
from rest_framework.response import Response

from skeleton.access.policies import require_credential_scope
from skeleton.api.errors import BoundaryError, parse_revision
from skeleton.api.openapi import TableRoutes
from skeleton.api.parsers import BoundedJSONParser, BoundedMultiPartParser
from skeleton.api.views import TenantAPI, etag, request_fields
from skeleton.web.services import exposed_spec

from . import services

FILES_READ = "files:read"
FILES_WRITE = "files:write"


def _translate(error):
    if isinstance(error, services.UploadTooLarge):
        return BoundaryError("request_too_large", "O arquivo excede o limite permitido.", 413)
    if isinstance(error, services.QuotaExceeded):
        return BoundaryError("quota_exceeded", "A cota de armazenamento da organização foi atingida.", 409)
    if isinstance(error, services.UnsupportedFileType):
        return BoundaryError("unsupported_type", "Tipo de arquivo não permitido.", 400)
    return error


class AttachmentsView(TenantAPI):
    parser_classes = [BoundedMultiPartParser]
    token_methods = ("GET", "POST")
    openapi = TableRoutes("attachments")

    def get(self, request, organization_id, table_key, sysid):
        require_credential_scope(FILES_READ)
        spec = exposed_spec(table_key)
        rows = services.attachments_for(spec, sysid, user=request.user)
        return Response({"results": [services.attachment_data(row) for row in rows]})

    @transaction.atomic
    def post(self, request, organization_id, table_key, sysid):
        require_credential_scope(FILES_WRITE)
        spec = exposed_spec(table_key)
        services.policy(spec)
        uploaded = request.FILES.get("file")
        if uploaded is None or set(request.FILES) != {"file"} or set(request.data) - {"file"}:
            raise BoundaryError("invalid_request", "Envie exatamente um campo file.")
        try:
            attachment = services.upload(
                spec, sysid, user=request.user, stream=uploaded, filename=uploaded.name, size=uploaded.size
            )
        except (services.UploadTooLarge, services.UnsupportedFileType, services.QuotaExceeded) as error:
            raise _translate(error) from None
        return etag(Response(services.attachment_data(attachment), status=201), attachment.revision)


class AttachmentDetailView(TenantAPI):
    parser_classes = [BoundedJSONParser]
    token_methods = ("DELETE",)
    openapi = TableRoutes("attachment")

    @transaction.atomic
    def delete(self, request, organization_id, table_key, sysid, attachment_id):
        require_credential_scope(FILES_WRITE)
        if request_fields(request):
            raise BoundaryError("invalid_request", "Remoção não aceita campos.")
        revision = parse_revision(request.headers.get("If-Match"))
        services.remove(exposed_spec(table_key), sysid, attachment_id, user=request.user, revision=revision)
        return Response(status=204)


class AttachmentRestoreView(TenantAPI):
    parser_classes = [BoundedJSONParser]
    token_methods = ("POST",)
    openapi = TableRoutes("attachment-restore")

    @transaction.atomic
    def post(self, request, organization_id, table_key, sysid, attachment_id):
        require_credential_scope(FILES_WRITE)
        if request_fields(request):
            raise BoundaryError("invalid_request", "Restauração não aceita campos.")
        revision = parse_revision(request.headers.get("If-Match"))
        try:
            attachment = services.restore(
                exposed_spec(table_key), sysid, attachment_id, user=request.user, revision=revision
            )
        except services.QuotaExceeded as error:
            raise _translate(error) from None
        return etag(Response(services.attachment_data(attachment)), attachment.revision)
