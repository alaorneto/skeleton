"""REST for comments under the target record: list/create, edit and delete with If-Match, restore."""

from django.db import transaction
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError, parse_revision
from skeleton.api.openapi import TableRoutes
from skeleton.api.parsers import BoundedJSONParser
from skeleton.api.views import TenantAPI, etag, request_fields
from skeleton.web.services import exposed_spec

from . import services


def _body(request):
    data = request_fields(request)
    if set(data) != {"body"}:
        raise BoundaryError("invalid_request", "Envie só o campo body.")
    return data["body"]


class CommentsView(TenantAPI):
    parser_classes = [BoundedJSONParser]
    token_methods = ("GET", "POST")
    openapi = TableRoutes("comments")

    def get(self, request, organization_id, table_key, sysid):
        rows = services.comments_for(exposed_spec(table_key), sysid, user=request.user)
        return Response({"results": [services.comment_data(row) for row in rows]})

    @transaction.atomic
    def post(self, request, organization_id, table_key, sysid):
        comment = services.add(exposed_spec(table_key), sysid, user=request.user, body=_body(request))
        return etag(Response(services.comment_data(comment), status=201), comment.revision)


class CommentDetailView(TenantAPI):
    parser_classes = [BoundedJSONParser]
    token_methods = ("PATCH", "DELETE")
    openapi = TableRoutes("comment")

    @transaction.atomic
    def patch(self, request, organization_id, table_key, sysid, comment_id):
        revision = parse_revision(request.headers.get("If-Match"))
        comment = services.edit(
            exposed_spec(table_key), sysid, comment_id, user=request.user, body=_body(request), revision=revision
        )
        return etag(Response(services.comment_data(comment)), comment.revision)

    @transaction.atomic
    def delete(self, request, organization_id, table_key, sysid, comment_id):
        if request_fields(request):
            raise BoundaryError("invalid_request", "Exclusão não aceita campos.")
        revision = parse_revision(request.headers.get("If-Match"))
        services.remove(exposed_spec(table_key), sysid, comment_id, user=request.user, revision=revision)
        return Response(status=204)


class CommentRestoreView(TenantAPI):
    parser_classes = [BoundedJSONParser]
    token_methods = ("POST",)
    openapi = TableRoutes("comment-restore")

    @transaction.atomic
    def post(self, request, organization_id, table_key, sysid, comment_id):
        if request_fields(request):
            raise BoundaryError("invalid_request", "Restauração não aceita campos.")
        revision = parse_revision(request.headers.get("If-Match"))
        comment = services.restore(exposed_spec(table_key), sysid, comment_id, user=request.user, revision=revision)
        return etag(Response(services.comment_data(comment)), comment.revision)
