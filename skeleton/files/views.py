"""HTML attachment actions from the record detail page (plain multipart forms, no JavaScript)."""

from django import forms
from django.conf import settings
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.web.services import exposed_spec
from skeleton.web.views import tenant_page

from . import services


class RevisionForm(forms.Form):
    revision = forms.IntegerField(min_value=1)


def _detail(organization_id, table_key, sysid):
    return redirect("web:record", organization_id=organization_id, table_key=table_key, sysid=sysid)


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
    if length > settings.SKELETON_UPLOAD_MAX_BYTES + 64 * 1024:
        return render(
            request, "skeleton/web/error.html", {"detail": "O arquivo excede o limite permitido."}, status=413
        )
    uploaded = request.FILES.get("file")
    if uploaded is None or set(request.FILES) != {"file"} or set(request.POST) - {"csrfmiddlewaretoken"}:
        return render(request, "skeleton/web/error.html", {"detail": "Selecione um arquivo."}, status=400)
    try:
        services.upload(spec, sysid, user=request.user, stream=uploaded, filename=uploaded.name, size=uploaded.size)
    except services.UploadTooLarge:
        return render(
            request, "skeleton/web/error.html", {"detail": "O arquivo excede o limite permitido."}, status=413
        )
    return _detail(organization_id, table_key, sysid)


@tenant_page()
@require_POST
@transaction.atomic
def remove(request, organization_id, table_key, sysid, attachment_id):
    form = RevisionForm(request.POST)
    if set(request.POST) - {"revision", "csrfmiddlewaretoken"} or not form.is_valid():
        return render(request, "skeleton/web/error.html", {"detail": "Revisão inválida."}, status=400)
    try:
        services.remove(
            exposed_spec(table_key), sysid, attachment_id, user=request.user, revision=form.cleaned_data["revision"]
        )
    except ConcurrentUpdate:
        return render(
            request, "skeleton/web/error.html", {"detail": "O anexo foi alterado. Atualize a página."}, status=409
        )
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
    rows = [services.attachment_data(row) for row in services.attachments_for(spec, record.pk, user=user)]
    return {"rows": rows, "can_write": can_write, "types": ", ".join(spec.attachments.types)}
