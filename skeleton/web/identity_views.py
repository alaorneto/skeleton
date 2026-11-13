"""``/o/<org>/identity/``: the organization's view of its SSO providers and their admission rules (#31)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from skeleton.access.capabilities import IDENTITY_CONFIGURE
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy import identity_admission

from .views import tenant_page


class ProviderAdmissionForm(forms.Form):
    jit_provisioning = forms.BooleanField(
        label="Criar acesso no primeiro login (JIT)",
        required=False,
        help_text="Sem isso, só entra quem já é membro ou foi convidado.",
    )
    jit_requires_approval = forms.BooleanField(
        label="Pedir aprovação antes",
        required=False,
        help_text="O primeiro login cria um pedido de ingresso em vez do vínculo.",
    )
    default_role = forms.ModelChoiceField(
        label="Papel padrão",
        queryset=None,
        required=False,
        empty_label="Membro (sem permissões)",
        help_text="Nunca vem de um atributo do provedor.",
    )
    allowed_email_domains = forms.CharField(
        label="Domínios de email permitidos",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Um por linha, até 50. Em branco, qualquer email que o provedor ateste.",
    )
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        from skeleton.access.models import Role

        super().__init__(*args, **kwargs)
        field = self.fields["default_role"]
        field.queryset = Role.objects.order_by("name")
        field.label_from_instance = lambda role: role.name


def _initial(item):
    row = item.admission
    return {
        "jit_provisioning": bool(row and row.jit_provisioning),
        "jit_requires_approval": bool(row and row.jit_requires_approval),
        "default_role": row.default_role_id if row else None,
        "allowed_email_domains": "\n".join(row.allowed_email_domains) if row else "",
        "revision": item.revision,
    }


def _page(request, organization_id, *, forms_by_id=None, status=200, error=None):
    items = identity_admission.providers()
    forms_by_id = forms_by_id or {}
    dashboard = reverse("web:dashboard", kwargs={"organization_id": organization_id})
    return render(
        request,
        "skeleton/web/identity.html",
        {
            "organization_id": organization_id,
            "rows": [
                {
                    "item": item,
                    "form": forms_by_id.get(item.sysid)
                    or ProviderAdmissionForm(initial=_initial(item), prefix=str(item.sysid)),
                }
                for item in items
            ],
            "error": error,
            "breadcrumbs": [{"label": "Início", "url": dashboard}, {"label": "Identidade"}],
        },
        status=status,
    )


@tenant_page(capability=IDENTITY_CONFIGURE)
@require_http_methods(["GET"])
def identity(request, organization_id):
    return _page(request, organization_id)


@tenant_page(capability=IDENTITY_CONFIGURE)
@require_POST
def identity_admission_update(request, organization_id, sysid):
    try:
        item = identity_admission.provider(sysid)
    except identity_admission.ProviderNotFound:
        raise Http404 from None
    form = ProviderAdmissionForm(request.POST, prefix=str(item.sysid))
    if not form.is_valid():
        return _page(request, organization_id, forms_by_id={item.sysid: form}, status=400)
    data = dict(form.cleaned_data)
    revision = data.pop("revision")
    try:
        with transaction.atomic():
            identity_admission.configure(
                actor=request.user, provider_sysid=item.sysid, data=data, expected_revision=revision
            )
    except identity_admission.ProviderNotFound:
        raise Http404 from None
    except PermissionDenied:
        form.add_error("default_role", "Só o administrador escolhe um papel com capacidades administrativas.")
        return _page(request, organization_id, forms_by_id={item.sysid: form}, status=403)
    except ConcurrentUpdate:
        form.add_error(None, "As regras mudaram desde que a página foi aberta. Atualize e tente de novo.")
        return _page(request, organization_id, forms_by_id={item.sysid: form}, status=409)
    except ValidationError as exc:
        for field, problems in exc.message_dict.items():
            form.add_error(field if field in form.fields else None, problems)
        return _page(request, organization_id, forms_by_id={item.sysid: form}, status=400)
    messages.success(request, f"Regras de {item.name} salvas.")
    return redirect("web:identity", organization_id=organization_id)
