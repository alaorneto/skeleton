"""``/profile/privacy/``: the person downloads their own data (#190) and asks for erasure (#191).

The page explains what the export contains; the download is an aggregate route (no single
tenant transaction) that builds the ZIP for the authenticated person only and returns it as an
attachment, never cached. The erasure request is opened and cancelled by the person only; a
platform administrator decides it at ``/platform/privacy/``. Every action requires a recent
authentication, as for other account actions.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.aggregate import aggregate_route


class ErasureForm(forms.Form):
    confirm = forms.BooleanField(
        label=gettext_lazy("Entendo que a eliminação é irreversível e que perco o acesso a todas as organizações.")
    )


class CancelErasureForm(forms.Form):
    request = forms.UUIDField()
    expected_revision = forms.IntegerField(min_value=1)


@login_required
@never_cache
@require_safe
def privacy(request):
    from skeleton.accounts.erasure import current_request

    return render(
        request,
        "skeleton/web/privacy.html",
        {"erasure": current_request(request.user), "erasure_form": ErasureForm()},
    )


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


@login_required
@never_cache
@require_POST
@recent_authentication_required
def privacy_erasure(request):
    from skeleton.accounts.erasure import current_request, request_erasure

    form = ErasureForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "skeleton/web/privacy.html",
            {"erasure": current_request(request.user), "erasure_form": form},
            status=400,
        )
    try:
        request_erasure(request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request, gettext("Pedido de eliminação enviado. A administração da plataforma vai analisá-lo.")
        )
    return redirect("web:privacy")


@login_required
@never_cache
@require_POST
@recent_authentication_required
def privacy_erasure_cancel(request):
    from skeleton.accounts.erasure import cancel_erasure

    form = CancelErasureForm(request.POST)
    if not form.is_valid():
        messages.error(request, gettext("Pedido inválido. Atualize a página e tente de novo."))
        return redirect("web:privacy")
    try:
        cancel_erasure(
            request.user,
            request_id=form.cleaned_data["request"],
            expected_revision=form.cleaned_data["expected_revision"],
        )
    except (ValidationError, ConcurrentUpdate):
        messages.error(request, gettext("O pedido mudou ou já foi decidido. Atualize a página."))
    else:
        messages.success(request, gettext("Pedido de eliminação cancelado."))
    return redirect("web:privacy")
