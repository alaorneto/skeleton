"""Native client releases at /platform/native-clients/ (#606), for the release operator.

The platform administrator and the release operator (``updates.operate``) see what each
channel serves and the sync history, set the minimum version of a channel and withdraw a
version. Every POST needs CSRF, a recent authentication and the revision the page showed; the
changes are row changes of audited tables. Sources, tokens and keys stay in the operator's file.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.shortcuts import redirect, render
from django.utils.translation import gettext
from django.views.decorators.http import require_http_methods, require_safe

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.system.native_clients import services

from .guards import platform_page

OPERATE = "updates.operate"


class MinimumForm(forms.Form):
    minimum_version = forms.CharField(required=False, max_length=64)
    revision = forms.IntegerField(min_value=0)


class RevisionForm(forms.Form):
    revision = forms.IntegerField(min_value=1)


@platform_page(capability=OPERATE, writes=True)
@require_safe
def native_clients(request):
    try:
        clients, problem = services.overview(), None
    except ImproperlyConfigured:
        clients, problem = [], gettext("A configuração dos clientes nativos não pôde ser lida.")
    return render(
        request,
        "skeleton/web/platform/native_clients.html",
        {"platform_section": "native-clients", "clients": clients, "problem": problem},
    )


@platform_page(capability=OPERATE, writes=True)
@require_http_methods(["POST"])
@recent_authentication_required
def native_client_minimum(request, client_id, channel):
    form = MinimumForm(request.POST)
    if not form.is_valid():
        messages.error(request, gettext("Pedido inválido. Atualize a página e tente de novo."))
        return redirect("platform:native-clients")
    try:
        services.set_minimum_version(
            actor=request.user,
            client_id=client_id,
            channel=channel,
            minimum_version=form.cleaned_data["minimum_version"],
            expected_revision=form.cleaned_data["revision"],
        )
    except ValidationError:
        messages.error(request, gettext("Versão ou canal inválido."))
    else:
        messages.success(request, gettext("Versão mínima do canal atualizada."))
    return redirect("platform:native-clients")


@platform_page(capability=OPERATE, writes=True)
@require_http_methods(["POST"])
@recent_authentication_required
def native_client_withdraw(request, sysid):
    form = RevisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, gettext("Pedido inválido. Atualize a página e tente de novo."))
        return redirect("platform:native-clients")
    try:
        services.withdraw_release(actor=request.user, release_id=sysid, expected_revision=form.cleaned_data["revision"])
    except ValidationError:
        messages.error(request, gettext("Versão desconhecida."))
    else:
        messages.success(request, gettext("Versão retirada do canal."))
    return redirect("platform:native-clients")
