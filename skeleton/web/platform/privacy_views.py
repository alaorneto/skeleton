"""Erasure requests at /platform/privacy/ (#191): explicit views over ``skeleton.accounts.erasure``.

Only the platform administrator sees and decides them. The list shows who asked (by the identity's
current email, which becomes ``anon-…@erased.invalid`` once anonymized), the status and the dates. Each
decision needs a recent authentication, POST with CSRF and the revision the administrator saw
(CAS); GET never changes state. Approving anonymizes the identity at once, which cannot be undone.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_POST, require_safe

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate

from .guards import platform_page


class DecisionForm(forms.Form):
    decision = forms.ChoiceField(choices=(("approve", gettext_lazy("Aprovar")), ("reject", gettext_lazy("Recusar"))))
    revision = forms.IntegerField(min_value=1)


@platform_page()
@require_safe
def privacy(request: HttpRequest) -> HttpResponse:
    from skeleton.accounts.models import ErasureRequest

    rows = ErasureRequest.objects.select_related("user").order_by("-created_at")[:200]
    return render(
        request,
        "skeleton/web/platform/privacy.html",
        {"platform_section": "privacy", "rows": rows, "open_states": ErasureRequest.OPEN},
    )


@platform_page()
@require_POST
@recent_authentication_required
def privacy_decide(request: Any, sysid: Any) -> HttpResponse:
    from skeleton.accounts.erasure import decide_erasure
    from skeleton.accounts.models import ErasureRequest

    form = DecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, gettext("Decisão inválida."))
        return redirect("platform:privacy")
    try:
        decide_erasure(
            actor=request.user,
            request_id=sysid,
            approve=form.cleaned_data["decision"] == "approve",
            expected_revision=form.cleaned_data["revision"],
        )
    except ErasureRequest.DoesNotExist:
        messages.error(request, gettext("Pedido indisponível."))
    except ConcurrentUpdate:
        messages.error(request, gettext("O pedido mudou; confira a lista antes de decidir."))
    except (ValidationError, PermissionDenied) as error:
        messages.error(request, "; ".join(getattr(error, "messages", None) or [str(error)]))
    else:
        if form.cleaned_data["decision"] == "approve":
            messages.success(request, gettext("Pedido aprovado: a identidade foi anonimizada."))
        else:
            messages.success(request, gettext("Pedido recusado."))
    return redirect("platform:privacy")
