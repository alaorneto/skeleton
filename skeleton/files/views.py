"""HTML attachment actions from the record detail page (plain multipart forms, no JavaScript)."""

from django import forms
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect
from django.utils.translation import gettext
from django.views.decorators.http import require_POST, require_safe

from skeleton.common.exceptions import ConcurrentUpdate, RecordSealed
from skeleton.records.services import exposed_spec
from skeleton.web.errors import error_response
from skeleton.web.views import portal_page, tenant_page

from . import services


class RevisionForm(forms.Form):
    revision = forms.IntegerField(min_value=1)


def _detail(organization_id, table_key, sysid):
    return redirect("web:record", organization_id=organization_id, table_key=table_key, sysid=sysid)


def _too_large():
    from django.template.defaultfilters import filesizeformat

    return gettext("O arquivo excede o limite de %(size)s.") % {
        "size": filesizeformat(settings.SKELETON_UPLOAD_MAX_BYTES)
    }


def _refused(request, back, message):
    """A refused upload goes back to the record with the reason, keeping its context (#473).

    The table policy was checked before; the record page revalidates the access as usual.
    """
    messages.error(request, message)
    return back


@tenant_page()
@require_POST
@transaction.atomic
def upload(request, organization_id, table_key, sysid):
    spec = exposed_spec(table_key)
    services.policy(spec)
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    back = _detail(organization_id, table_key, sysid)
    if length > settings.SKELETON_UPLOAD_MAX_BYTES + 64 * 1024:
        return _refused(request, back, _too_large())
    uploaded = request.FILES.get("file")
    if getattr(request, "skeleton_upload_exceeded", False):  # Stopped by BoundedUploadHandler (#242).
        return _refused(request, back, _too_large())
    if uploaded is None or set(request.FILES) != {"file"} or set(request.POST) - {"csrfmiddlewaretoken"}:
        return _refused(request, back, gettext("Selecione um arquivo."))
    try:
        services.upload(spec, sysid, user=request.user, stream=uploaded, filename=uploaded.name, size=uploaded.size)
    except services.UploadTooLarge:
        return _refused(request, back, _too_large())
    except services.QuotaExceeded:
        return _refused(request, back, gettext("A cota de armazenamento da organização foi atingida."))
    except RecordSealed:
        return _refused(request, back, gettext("O registro está selado: os anexos não podem mais mudar."))
    return _detail(organization_id, table_key, sysid)


@tenant_page()
@require_POST
@transaction.atomic
def remove(request, organization_id, table_key, sysid, attachment_id):
    form = RevisionForm(request.POST)
    if set(request.POST) - {"revision", "csrfmiddlewaretoken"} or not form.is_valid():
        return error_response(request, gettext("Revisão inválida."), status=400)
    try:
        services.remove(
            exposed_spec(table_key), sysid, attachment_id, user=request.user, revision=form.cleaned_data["revision"]
        )
    except ConcurrentUpdate:
        return error_response(request, gettext("O anexo foi alterado. Atualize a página."), status=409)
    except RecordSealed:
        return error_response(request, gettext("O registro está selado: os anexos não podem mais mudar."), status=409)
    return _detail(organization_id, table_key, sysid)


def panel_context(spec, record, *, user):
    """Attachment panel for the record detail; None when the table did not opt in."""
    from django.core.exceptions import PermissionDenied

    from skeleton.access.policies import require_record_action

    if spec.attachments is None:
        return None
    try:
        require_record_action(record, user=user, action="update")
        can_write = True
    except PermissionDenied:
        can_write = False
    from django.urls import reverse

    rows = []
    for row in services.attachments_for(spec, record.pk, user=user):
        data = services.attachment_data(row)
        data["download_url"] = reverse(
            "files:download",
            kwargs={
                "organization_id": record.organization_id,
                "table_key": spec.model._meta.label_lower,
                "sysid": record.pk,
                "attachment_id": row.pk,
            },
        )
        rows.append(data)
    return {"rows": rows, "can_write": can_write, "types": ", ".join(spec.attachments.types)}


@tenant_page()
def download(request, organization_id, table_key, sysid, attachment_id):
    """Authorize on every request; the bytes stream only after the transaction (DetachedFileResponse)."""
    from django.http import Http404

    from skeleton.common.responses import DetachedFileResponse

    from .models import Attachment

    # Any authorizable table: an app shows the attachments of a derived table on its own pages (#563).
    rows = services.attachments_for(services.attachment_spec(table_key), sysid, user=request.user)
    attachment = rows.filter(pk=attachment_id, state=Attachment.State.AVAILABLE).first()
    if attachment is None:
        raise Http404  # Missing, deleted, pending, quarantined and unauthorized look the same.
    return DetachedFileResponse(
        storage_key=attachment.storage_key, filename=attachment.filename, content_type=attachment.content_type
    )


@portal_page
@require_safe
def portal_download(request, organization_id, attachment_id):
    """An attachment for an external person (#534): the table's external reader decides, on every request."""
    from skeleton.common.responses import DetachedFileResponse

    attachment = services.external_attachment(attachment_id, user=request.user)
    return DetachedFileResponse(
        storage_key=attachment.storage_key, filename=attachment.filename, content_type=attachment.content_type
    )
