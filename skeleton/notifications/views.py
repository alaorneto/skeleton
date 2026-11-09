"""Inbox pages and the session API; work without JavaScript (plain forms with CSRF)."""

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from rest_framework.response import Response

from skeleton.api.errors import BoundaryError
from skeleton.api.views import SessionAPI, TenantAPI, request_fields
from skeleton.web.views import pagination, tenant_page

from . import inbox


def _back(request, organization_id):
    if request.POST.get("from") == "global":
        return redirect("notifications:global")
    return redirect("notifications:organization", organization_id=organization_id)


def _render_inbox(request, *, organization_id=None):
    include_archived = request.GET.get("archived") == "1"
    filters = {"archived": "1"} if include_archived else {}
    rows = pagination(
        request, inbox.inbox_queryset(request.user, include_archived=include_archived), validated_filters=filters
    )
    rows.object_list = [inbox.row_data(row) for row in rows]
    return render(
        request,
        "skeleton/notifications/inbox.html",
        {
            "title": "Notificações",
            "rows": rows,
            "organization_id": organization_id,
            "scope": "organization" if organization_id else "global",
            "include_archived": include_archived,
        },
    )


@login_required
def global_inbox(request):
    return _render_inbox(request)


@tenant_page()
def organization_inbox(request, organization_id):
    return _render_inbox(request, organization_id=organization_id)


@tenant_page()
@require_POST
@transaction.atomic
def change(request, organization_id, sysid, action):
    if action == "read":
        inbox.mark_read(request.user, [sysid])
    elif action == "unread":
        inbox.mark_read(request.user, [sysid], read=False)
    elif action == "archive":
        inbox.archive(request.user, [sysid])
    else:
        raise Http404
    return _back(request, organization_id)


@tenant_page()
@require_POST
@transaction.atomic
def read_all(request, organization_id):
    inbox.mark_all_read(request.user)
    return _back(request, organization_id)


@tenant_page()
@transaction.atomic
def open_notification(request, organization_id, sysid):
    url = inbox.open_target(request.user, sysid)
    if url is not None:
        return redirect(url)
    # Lost access or removed target: a generic page that reveals no title or snapshot.
    return render(
        request,
        "skeleton/notifications/unavailable.html",
        {"title": "Conteúdo indisponível", "organization_id": organization_id},
    )


class InboxAPI(SessionAPI):
    def get(self, request):
        from skeleton.api.views import page

        if set(request.query_params) - {"page", "page_size", "archived"}:
            raise BoundaryError("invalid_filter", "Filtro não permitido.")
        archived = request.query_params.get("archived", "0")
        if archived not in {"0", "1"}:
            raise BoundaryError("invalid_filter", "Filtro inválido.")
        response = page(inbox.inbox_queryset(request.user, include_archived=archived == "1"), request, inbox.row_data)
        response.data["unread"] = inbox.unread_count(request.user)
        return response


class InboxActionAPI(TenantAPI):
    ACTIONS = {"read", "unread", "archive", "read_all"}

    @transaction.atomic
    def post(self, request, organization_id):
        data = request_fields(request)
        action = data.get("action")
        if action not in self.ACTIONS or set(data) - {"action", "ids"}:
            raise BoundaryError("invalid_request", "Informe action e ids.")
        if action == "read_all":
            changed = inbox.mark_all_read(request.user)
        else:
            ids = data.get("ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
                raise BoundaryError("invalid_request", "Informe de 1 a 100 ids.")
            if action == "archive":
                changed = inbox.archive(request.user, ids)
            else:
                changed = inbox.mark_read(request.user, ids, read=action == "read")
        return Response({"changed": changed, "unread": inbox.unread_count(request.user)})


def inbox_url(organization_id=None):
    if organization_id is None:
        return reverse("notifications:global")
    return reverse("notifications:organization", kwargs={"organization_id": organization_id})
