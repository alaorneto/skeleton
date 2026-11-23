"""Clients of the platform API (#709) at /platform/api-clients/: create, issue and revoke tokens.

Only the platform administrator, with recent authentication. The token text appears once, in
the answer of the issue, with ``Cache-Control: no-store``.
"""

from __future__ import annotations

from datetime import timedelta

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.integrations.tokens import platform as clients
from skeleton.integrations.tokens.scopes import PLATFORM_SCOPES
from skeleton.web.views import form_error

from .guards import platform_page


class ClientForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=150)


class PlatformTokenForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome do token"), max_length=150)
    scopes = forms.MultipleChoiceField(
        label=gettext_lazy("Escopos"),
        choices=sorted(PLATFORM_SCOPES.items()),
        widget=forms.CheckboxSelectMultiple,
    )
    days = forms.IntegerField(label=gettext_lazy("Validade (dias)"), min_value=1, max_value=3650, initial=90)
    allowed_networks = forms.CharField(
        label=gettext_lazy("Redes de origem"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=gettext_lazy("CIDRs separados por linha ou vírgula; vazio aceita qualquer endereço."),
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def api_clients(request):
    form = ClientForm(request.POST or None)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    client = clients.create_platform_client(actor=request.user, name=form.cleaned_data["name"])
            except ValidationError as exc:
                status = form_error(form, exc)
            else:
                messages.success(request, gettext("Cliente criado. Emita um token para ele."))
                return redirect("platform:api-client", sysid=client.pk)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/platform/api_clients.html",
        {"platform_section": "api-clients", "clients": clients.platform_clients(), "form": form},
        status=status,
    )


def _client_page(request, client, *, form=None, issued=None, status=200):
    response = render(
        request,
        "skeleton/web/platform/api_client.html",
        {
            "platform_section": "api-clients",
            "client": client,
            "tokens": clients.platform_tokens(client.pk),
            "form": form or PlatformTokenForm(),
            "issued": issued,
        },
        status=status,
    )
    response["Cache-Control"] = "no-store"
    return response


def _get_client(sysid):
    from skeleton.accounts.models import AccountType, User

    return get_object_or_404(User.all_objects, pk=sysid, account_type=AccountType.PLATFORM_CLIENT)


@platform_page
@never_cache
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def api_client(request, sysid):
    """The client's tokens; POST issues one (the text shows once) or revokes one (``action=revoke``)."""
    client = _get_client(sysid)
    if request.method != "POST":
        return _client_page(request, client)
    if request.POST.get("action") == "revoke":
        try:
            with transaction.atomic():
                clients.revoke_platform_token(
                    actor=request.user,
                    token_id=request.POST.get("token"),
                    expected_revision=int(request.POST.get("revision", -1)),
                )
        except (ConcurrentUpdate, ValueError, ValidationError, LookupError):
            messages.error(request, gettext("O token mudou ou não existe mais."))
            return redirect("platform:api-client", sysid=client.pk)
        messages.success(request, gettext("Token revogado: a próxima requisição com ele recebe 401."))
        return redirect("platform:api-client", sysid=client.pk)
    form = PlatformTokenForm(request.POST)
    if not form.is_valid():
        return _client_page(request, client, form=form, status=400)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            issued = clients.issue_platform_token(
                actor=request.user,
                client_id=client.pk,
                name=data["name"],
                scopes=list(data["scopes"]),
                expires_at=timezone.now() + timedelta(days=data["days"]),
                allowed_networks=data["allowed_networks"],
            )
    except ValidationError as exc:
        status = form_error(form, exc)
        return _client_page(request, client, form=form, status=status)
    return _client_page(request, client, issued=issued.text)


@platform_page
@require_POST
@recent_authentication_required
@transaction.atomic
def api_client_deactivate(request, sysid):
    client = _get_client(sysid)
    clients.deactivate_platform_client(actor=request.user, client_id=client.pk)
    messages.success(request, gettext("Cliente desativado: os tokens dele deixam de valer."))
    return redirect("platform:api-clients")
