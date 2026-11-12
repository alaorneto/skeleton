"""Pages of CSV exports (#182): request an export, list my transfers, download the file.

Only the requester sees and downloads a transfer (anyone else gets 404, as if it did not exist);
every download rereads the requester's read permission on the table, so a revocation after the
export stops the download. The file leaves through ``DetachedFileResponse``, always as an
attachment with ``nosniff``, ``sandbox`` and ``no-store``.
"""

from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from skeleton.web.views import tenant_page


@tenant_page()
@require_POST
def export_request(request, organization_id, table_key):
    from .exports import request_export

    params = {name: value for name, value in request.POST.items() if name != "csrfmiddlewaretoken"}
    try:
        with transaction.atomic():
            request_export(user=request.user, table_key=table_key, params=params)
    except PermissionDenied:
        raise Http404 from None
    except ValidationError:
        return render(
            request, "skeleton/web/error.html", {"detail": gettext("Verifique os filtros informados.")}, status=400
        )
    messages.success(request, gettext("Exportação na fila. O arquivo aparece aqui quando estiver pronto."))
    return redirect("transfer:list", organization_id=organization_id)


@tenant_page()
@never_cache
@require_safe
def transfer_list(request, organization_id):
    from .models import TransferJob

    transfers = TransferJob.objects.filter(requester=request.user, expires_at__gt=timezone.now()).order_by(
        "-created_at"
    )[:50]
    return render(
        request,
        "skeleton/transfer/list.html",
        {"organization_id": organization_id, "transfers": transfers, "ready": TransferJob.State.READY},
    )


@tenant_page()
@require_safe
def transfer_download(request, organization_id, sysid):
    from skeleton.common.responses import DetachedFileResponse
    from skeleton.web.services import record_queryset

    from .exports import exportable_spec
    from .models import TransferJob

    transfer = TransferJob.objects.filter(
        pk=sysid, requester=request.user, state=TransferJob.State.READY, expires_at__gt=timezone.now()
    ).first()
    if transfer is None or not transfer.file_key:
        raise Http404
    try:
        record_queryset(exportable_spec(transfer.table_key), user=request.user, action="read")
    except PermissionDenied:
        raise Http404 from None
    return DetachedFileResponse(storage_key=transfer.file_key, filename=transfer.filename, content_type="text/csv")
