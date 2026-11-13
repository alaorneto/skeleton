"""``/profile/privacy/``: the person downloads their own data (#190) and asks for erasure (#191).

The page explains what the export contains; the download is an aggregate route (no single
tenant transaction) that builds the ZIP for the authenticated person only and returns it as an
attachment, never cached. A recent authentication is required, as for other account actions.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.tenancy.aggregate import aggregate_route


@login_required
@never_cache
@require_safe
def privacy(request):
    from skeleton.accounts.erasure import open_request

    return render(request, "skeleton/web/privacy.html", {"erasure": open_request(request.user)})


@aggregate_route
@login_required
@never_cache
@require_POST
@recent_authentication_required
def privacy_erasure(request):
    """Ask for (or withdraw a pending) anonymization; the platform approves it (#191)."""
    from skeleton.accounts.erasure import request_erasure, withdraw_erasure
    from skeleton.common.exceptions import ConcurrentUpdate
    from skeleton.tenancy.aggregate import user_context

    try:
        with user_context(request, source="http:privacy-erasure"):
            if request.POST.get("intent") == "withdraw":
                withdraw_erasure(
                    request.user,
                    request_id=request.POST.get("request", ""),
                    expected_revision=int(request.POST.get("revision") or 0),
                )
                messages.success(request, gettext("Pedido de eliminação retirado."))
            else:
                request_erasure(request.user)
                messages.success(request, gettext("Pedido enviado. Um administrador da plataforma vai analisá-lo."))
    except (ValidationError, ConcurrentUpdate, ValueError) as error:
        messages.error(request, "; ".join(getattr(error, "messages", None) or [gettext("Pedido indisponível.")]))
    return redirect("web:privacy")


@aggregate_route
@login_required
@never_cache
@require_safe
@recent_authentication_required
def privacy_export(request):
    from skeleton.accounts.privacy import export_my_data
    from skeleton.common.logging import security_event
    from skeleton.common.responses import protect

    content = export_my_data(request.user)
    security_event("privacy.export", target_sysid=request.user.pk, outcome="success")
    response = HttpResponse(content, content_type="application/zip")
    return protect(response, filename=f"meus-dados-{timezone.localdate().isoformat()}.zip")
