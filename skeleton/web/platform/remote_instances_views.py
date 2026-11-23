"""Connections to remote instances (#712) at /platform/remote-instances/: only the platform administrator."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.integrations.remote_instances import services
from skeleton.web.views import form_error

from .guards import platform_page


class ConnectionForm(forms.Form):
    label = forms.CharField(label=gettext_lazy("Nome"), max_length=200)
    base_url = forms.CharField(
        label=gettext_lazy("Endereço base"), max_length=300, help_text=gettext_lazy("https://… da instância remota.")
    )
    token = forms.CharField(
        label=gettext_lazy("Token de plataforma"),
        required=False,
        strip=True,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy("Emitido na instância remota em /platform/api-clients/; vazio mantém o gravado."),
    )
    granted_organization = forms.ModelChoiceField(
        label=gettext_lazy("Organização liberada"),
        queryset=None,
        required=False,
        empty_label=gettext_lazy("Nenhuma"),
        help_text=gettext_lazy("A única organização cujas apps leem esta conexão, em jobs."),
    )
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        from skeleton.tenancy.models import Organization

        super().__init__(*args, **kwargs)
        self.fields["granted_organization"].queryset = Organization.objects.filter(is_active=True).order_by("name")


def _page(request, connection, form, *, status=200, result=None):
    from skeleton.integrations.remote_instances.models import RemoteInstanceConnection

    return render(
        request,
        "skeleton/web/platform/remote_instances.html",
        {
            "platform_section": "remote-instances",
            "connections": RemoteInstanceConnection.objects.order_by("label"),
            "connection": connection,
            "form": form,
            "result": result,
        },
        status=status,
    )


def _initial(connection):
    if connection is None:
        return {"expected_revision": 0}
    return {
        "label": connection.label,
        "base_url": connection.base_url,
        "granted_organization": connection.granted_organization_id,
        "expected_revision": connection.revision,
    }


def _save(request, connection):
    form = ConnectionForm(request.POST)
    if not form.is_valid():
        return _page(request, connection, form, status=400)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            saved = services.save_connection(
                actor=request.user,
                label=data["label"],
                base_url=data["base_url"],
                token=data["token"],
                granted_organization=data["granted_organization"],
                sysid=connection.pk if connection is not None else None,
                expected_revision=data["expected_revision"],
            )
    except (ValidationError, ConcurrentUpdate) as exc:
        status = form_error(form, exc)
        return _page(request, connection, form, status=status)
    messages.success(request, gettext("Conexão salva. Teste para fixar a identidade da instância remota."))
    return redirect("platform:remote-instance", sysid=saved.pk)


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def remote_instances(request):
    if request.method == "POST":
        return _save(request, None)
    return _page(request, None, ConnectionForm(initial=_initial(None)))


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def remote_instance(request, sysid):
    from skeleton.integrations.remote_instances.models import RemoteInstanceConnection

    connection = get_object_or_404(RemoteInstanceConnection.objects, pk=sysid)
    if request.method != "POST":
        return _page(request, connection, ConnectionForm(initial=_initial(connection)))
    if request.POST.get("action") == "deactivate":
        try:
            services.deactivate_connection(
                actor=request.user, sysid=connection.pk, expected_revision=int(request.POST.get("revision", -1))
            )
        except (ConcurrentUpdate, ValueError):
            messages.error(request, gettext("A conexão mudou. Atualize a página."))
            return redirect("platform:remote-instance", sysid=connection.pk)
        messages.success(request, gettext("Conexão desativada."))
        return redirect("platform:remote-instances")
    return _save(request, connection)


@platform_page
@require_POST
@recent_authentication_required
@transaction.atomic
def remote_instance_test(request, sysid):
    from skeleton.integrations.remote_instances.models import RemoteInstanceConnection

    connection = get_object_or_404(RemoteInstanceConnection.objects, pk=sysid)
    code = services.test_connection(actor=request.user, sysid=connection.pk)
    connection.refresh_from_db()
    result = gettext("Conexão bem-sucedida.") if not code else gettext("Falhou: %(code)s.") % {"code": code}
    return _page(request, connection, ConnectionForm(initial=_initial(connection)), result=result)
