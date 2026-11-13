"""HTML comment actions from the record detail page (plain forms, no JavaScript)."""

from django import forms
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.records.services import exposed_spec
from skeleton.web.views import tenant_page

from . import services
from .text import linkify


class RevisionForm(forms.Form):
    revision = forms.IntegerField(min_value=1)


def _detail(request, organization_id, table_key, sysid):
    # The flow pages show the record's comments too (#421): back to them, never anywhere else.
    target = request.POST.get("next", "")
    inside = target.startswith(f"/o/{organization_id}/flows/") and "\\" not in target
    if inside and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect("web:record", organization_id=organization_id, table_key=table_key, sysid=sysid)


def _error(request, detail, status):
    return render(request, "skeleton/web/error.html", {"detail": detail}, status=status)


def panel_context(spec, record, *, user, next_url=""):
    """Comment panel of the record page; None when the table did not opt in."""
    if getattr(spec, "comments", None) is None:
        return None
    try:
        rows = services.comments_for(spec, record.pk, user=user, include_deleted=True)
    except Http404:
        return None
    moderator = services.is_moderator(user)
    items = []
    for row in rows:
        mine = row.created_by == user.pk
        items.append(
            {
                "sysid": row.pk,
                "revision": row.revision,
                "author_id": row.created_by,
                "created_at": row.created_at,
                "edited_at": row.edited_at,
                "deleted": row.deleted_at is not None,
                "body_html": linkify(row.body),
                "body": row.body,
                "can_edit": mine and row.deleted_at is None,
                "can_delete": (mine or moderator) and row.deleted_at is None,
                "can_restore": (mine or moderator) and row.deleted_at is not None,
            }
        )
    from .mentions import mentionable

    return {"rows": items, "mentionable": mentionable(spec, record), "next": next_url}


@tenant_page()
@require_POST
@transaction.atomic
def add(request, organization_id, table_key, sysid):
    if set(request.POST) - {"body", "next", "csrfmiddlewaretoken"}:
        return _error(request, gettext("Campo não permitido."), 400)
    services.add(exposed_spec(table_key), sysid, user=request.user, body=request.POST.get("body", ""))
    return _detail(request, organization_id, table_key, sysid)


@tenant_page()
@require_POST
@transaction.atomic
def edit(request, organization_id, table_key, sysid, comment_id):
    form = RevisionForm(request.POST)
    if set(request.POST) - {"body", "revision", "next", "csrfmiddlewaretoken"} or not form.is_valid():
        return _error(request, gettext("Revisão inválida."), 400)
    try:
        services.edit(
            exposed_spec(table_key),
            sysid,
            comment_id,
            user=request.user,
            body=request.POST.get("body", ""),
            revision=form.cleaned_data["revision"],
        )
    except ConcurrentUpdate:
        return _error(request, gettext("O comentário foi alterado. Atualize a página."), 409)
    return _detail(request, organization_id, table_key, sysid)


def _lifecycle(request, organization_id, table_key, sysid, comment_id, operation):
    form = RevisionForm(request.POST)
    if set(request.POST) - {"revision", "next", "csrfmiddlewaretoken"} or not form.is_valid():
        return _error(request, gettext("Revisão inválida."), 400)
    try:
        operation(exposed_spec(table_key), sysid, comment_id, user=request.user, revision=form.cleaned_data["revision"])
    except ConcurrentUpdate:
        return _error(request, gettext("O comentário foi alterado. Atualize a página."), 409)
    except PermissionDenied:
        raise
    return _detail(request, organization_id, table_key, sysid)


@tenant_page()
@require_POST
@transaction.atomic
def remove(request, organization_id, table_key, sysid, comment_id):
    return _lifecycle(request, organization_id, table_key, sysid, comment_id, services.remove)


@tenant_page()
@require_POST
@transaction.atomic
def restore(request, organization_id, table_key, sysid, comment_id):
    return _lifecycle(request, organization_id, table_key, sysid, comment_id, services.restore)
