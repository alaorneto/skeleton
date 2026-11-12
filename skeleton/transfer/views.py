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
from django.views.decorators.http import require_http_methods, require_POST, require_safe

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
        {
            "organization_id": organization_id,
            "transfers": transfers,
            "ready": TransferJob.State.READY,
            "previewed": TransferJob.State.PREVIEWED,
        },
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


@tenant_page()
@never_cache
@require_http_methods(["GET", "POST"])
def import_request(request, organization_id, table_key):
    """Upload a CSV; the preview job validates it without writing anything."""
    from skeleton.web.services import record_queryset

    from .exports import exportable_spec
    from .imports import CONTROL_COLUMNS, importable_columns, request_import

    try:
        spec = exportable_spec(table_key)
        record_queryset(spec, user=request.user, action="create")
    except PermissionDenied:
        raise Http404 from None
    errors = []
    status = 200
    if request.method == "POST":
        upload = request.FILES.get("file")
        if upload is None:
            errors, status = [gettext("Escolha um arquivo CSV.")], 400
        else:
            try:
                with transaction.atomic():
                    request_import(user=request.user, table_key=table_key, upload=upload)
            except PermissionDenied:
                raise Http404 from None
            except ValidationError as exc:
                errors, status = [message for items in exc.message_dict.values() for message in items], 400
            else:
                messages.success(request, gettext("Arquivo recebido. A prévia aparece aqui quando estiver pronta."))
                return redirect("transfer:list", organization_id=organization_id)
    return render(
        request,
        "skeleton/transfer/import.html",
        {
            "organization_id": organization_id,
            "table_key": table_key,
            "title": spec.label or spec.model._meta.verbose_name_plural,
            "columns": (*CONTROL_COLUMNS, *importable_columns(spec)),
            "errors": errors,
        },
        status=status,
    )


@tenant_page()
@require_POST
def import_confirm(request, organization_id, sysid):
    from .imports import confirm_import
    from .models import TransferJob

    transfer = TransferJob.objects.filter(pk=sysid, requester=request.user).first()
    if transfer is None:
        raise Http404
    try:
        with transaction.atomic():
            confirm_import(user=request.user, transfer=transfer)
    except PermissionDenied:
        raise Http404 from None
    except ValidationError:
        return render(
            request,
            "skeleton/web/error.html",
            {"detail": gettext("Só uma prévia sem erros pode ser aplicada.")},
            status=409,
        )
    messages.success(request, gettext("Importação na fila. O resultado aparece aqui."))
    return redirect("transfer:list", organization_id=organization_id)


@tenant_page()
@require_safe
def report_download(request, organization_id, sysid):
    from skeleton.common.responses import DetachedFileResponse

    from .models import TransferJob

    transfer = TransferJob.objects.filter(pk=sysid, requester=request.user, expires_at__gt=timezone.now()).first()
    if transfer is None or not transfer.report_key:
        raise Http404
    return DetachedFileResponse(
        storage_key=transfer.report_key,
        filename=f"erros-{transfer.filename or 'importacao.csv'}",
        content_type="text/csv",
    )
