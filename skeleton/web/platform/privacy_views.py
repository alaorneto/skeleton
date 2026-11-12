"""Erasure requests at /platform/privacy/ (#191): explicit views over ``skeleton.accounts.erasure``.

The list shows who asked, when and the state; a pending request is approved (which anonymizes
the identity at once and enqueues the cleanup of each organization) or rejected with a note. Both need a recent authentication, POST with CSRF and
the revision the operator saw (CAS). An administrator never decides their own request, and an
approval that would leave an organization or the platform without an administrator is refused.
GET never changes state.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods, require_safe

from skeleton.access.services import LastAdministrator
from skeleton.accounts.erasure import approve_erasure, reject_erasure, requests_for_platform
from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.web.views import pagination

from .guards import platform_page

STATES = (
    ("", gettext_lazy("Todos")),
    ("pending", gettext_lazy("Aguardando aprovação")),
    ("rejected", gettext_lazy("Recusado")),
    ("cancelled", gettext_lazy("Cancelado")),
    ("completed", gettext_lazy("Concluído")),
)


class ErasureFilterForm(forms.Form):
    state = forms.ChoiceField(required=False, choices=STATES)


class ErasureDecisionForm(forms.Form):
    action = forms.ChoiceField(choices=(("approve", gettext_lazy("Aprovar")), ("reject", gettext_lazy("Recusar"))))
    expected_revision = forms.IntegerField(min_value=1)
    note = forms.CharField(required=False, max_length=500)


@platform_page
@require_safe
def privacy(request):
    form = ErasureFilterForm(request.GET or None)
    state = form.cleaned_data.get("state") if form.is_valid() else None
    filters = {"state": state} if state else {}
    rows = pagination(request, requests_for_platform(actor=request.user, state=state), validated_filters=filters)
    return render(
        request,
        "skeleton/web/platform/privacy.html",
        {"platform_section": "privacy", "rows": rows, "filter_form": form},
    )


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
def privacy_decision(request, sysid):
    form = ErasureDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, gettext("Pedido inválido. Atualize a página e tente de novo."))
        return redirect("platform:privacy")
    data = form.cleaned_data
    try:
        if data["action"] == "approve":
            approve_erasure(actor=request.user, request_id=sysid, expected_revision=data["expected_revision"])
        else:
            reject_erasure(
                actor=request.user, request_id=sysid, expected_revision=data["expected_revision"], note=data["note"]
            )
    except ConcurrentUpdate:
        messages.error(request, gettext("O pedido mudou ou já foi decidido. Atualize a página."))
        return redirect("platform:privacy")
    except LastAdministrator:
        messages.error(
            request,
            gettext(
                "A pessoa é a última administradora de uma organização. Designe outro administrador antes de aprovar."
            ),
        )
        return redirect("platform:privacy")
    except (ValidationError, PermissionDenied) as exc:
        detail = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
        messages.error(request, " ".join(detail))
        return redirect("platform:privacy")
    messages.success(
        request,
        gettext("Pedido aprovado. A identidade foi anonimizada; a limpeza de cada organização foi enfileirada.")
        if data["action"] == "approve"
        else gettext("Pedido recusado."),
    )
    return redirect("platform:privacy")
