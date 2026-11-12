"""HTML of transfers: request an export from a list, follow it and download the file."""

from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST, require_safe

from skeleton.web.services import exposed_spec
from skeleton.web.views import pagination, tenant_page

from . import exports


@tenant_page()
@require_POST
@transaction.atomic
def request_export(request, organization_id, table_key):
    """POST from the list page; the hidden fields carry the list filters as they were."""
    spec = exposed_spec(table_key)
    filters = {name: value for name, value in request.POST.items() if name != "csrfmiddlewaretoken"}
    exports.request_export(spec, user=request.user, filters=filters)
    return redirect("transfer:transfers", organization_id=organization_id)


@tenant_page()
@require_safe
def transfers(request, organization_id):
    rows = pagination(request, exports.transfers_of(request.user))
    return render(
        request,
        "skeleton/transfer/transfers.html",
        {"title": "Exportações", "rows": rows, "organization_id": organization_id},
    )


@tenant_page()
@require_safe
def download(request, organization_id, sysid):
    """Re-authorized on every request: requester only, unexpired, still allowed to read the table."""
    from skeleton.common.responses import DetachedFileResponse

    transfer = exports.downloadable(sysid, user=request.user)
    return DetachedFileResponse(storage_key=transfer.storage_key, filename=transfer.filename, content_type="text/csv")
