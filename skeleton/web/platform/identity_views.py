"""``/platform/identity-providers/``: the platform registers each organization's IdP (#30).

Explicit forms over ``skeleton.accounts.identity_providers``; no SocialApp editor. Identity
fields appear read-only after creation and are refused if posted. Secrets are write-only: an
empty field keeps the stored value, and nothing renders them back. Every mutation requires
CSRF and a recent authentication; "Testar" writes only ``last_check_*``.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.web.views import form_error

from .guards import platform_page

__all__ = ["identity_provider_check", "identity_provider_create", "identity_provider_detail", "identity_providers"]

IMMUTABLE_POST_FIELDS = {
    "provider_id",
    "protocol",
    "bound_organization",
    "issuer",
    "entity_id",
    "client_id",
    "sp_entity_id",
}


def _textarea(rows=4):
    return forms.Textarea(attrs={"rows": rows, "spellcheck": "false", "autocomplete": "off"})


def _secret(label, help_text):
    return forms.CharField(
        label=label,
        required=False,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text=help_text,
    )


class _SamlOptions(forms.Form):
    idp_sso_url = forms.URLField(label="URL de SSO do IdP", required=False, assume_scheme="https")
    idp_slo_url = forms.URLField(label="URL de logout do IdP", required=False, assume_scheme="https")
    idp_metadata_url = forms.URLField(label="URL de metadata do IdP", required=False, assume_scheme="https")
    idp_x509cert = forms.CharField(
        label="Certificado do IdP", required=False, widget=_textarea(), help_text="PEM, público."
    )
    sp_x509cert = forms.CharField(
        label="Certificado do SP", required=False, widget=_textarea(), help_text="PEM, público."
    )

    def saml_settings(self):
        data = self.cleaned_data
        idp = {
            key: data[f"idp_{key}"].strip()
            for key in ("sso_url", "slo_url", "metadata_url", "x509cert")
            if data.get(f"idp_{key}")
        }
        sp = {"x509cert": data["sp_x509cert"].strip()} if data.get("sp_x509cert") else {}
        return {"idp": idp, "sp": sp, "attribute_mapping": {}}


class IdentityProviderCreateForm(_SamlOptions):
    protocol = forms.ChoiceField(choices=(("oidc", "OpenID Connect"), ("saml", "SAML 2.0")), widget=forms.HiddenInput)
    provider_id = forms.RegexField(
        label="Identificador (provider_id)",
        regex=r"^[a-z0-9][a-z0-9_-]{0,63}$",
        help_text="Aparece na URL de login. Não pode ser alterado depois.",
    )
    name = forms.CharField(label="Nome exibido", max_length=200)
    bound_organization = forms.ModelChoiceField(label="Organização", queryset=None)
    issuer = forms.URLField(
        label="Issuer (OIDC)",
        required=False,
        assume_scheme="https",
        help_text="URL do realm; a descoberta é lida dela.",
    )
    entity_id = forms.CharField(label="Entity ID do IdP (SAML)", required=False, max_length=500)
    client_id = forms.CharField(
        label="Client ID", max_length=255, help_text="Em SAML, o slug usado na URL de login do allauth."
    )
    sp_entity_id = forms.CharField(label="Entity ID do SP (SAML)", required=False, max_length=500)
    secret = _secret("Segredo do cliente (OIDC)", "Guardado cifrado; não é mostrado de novo.")
    private_key = _secret("Chave privada do SP (SAML)", "Opcional; guardada cifrada.")
    show_on_login = forms.BooleanField(label="Mostrar na tela de login", required=False, initial=True)
    trust_verified_email = forms.BooleanField(
        label="Confiar no email verificado pelo provedor",
        required=False,
        help_text="Só quando o provedor atesta o email; sem isso, a confirmação local continua obrigatória.",
    )

    field_order = ["protocol", "provider_id", "name", "bound_organization", "issuer", "entity_id", "client_id"]

    def __init__(self, *args, protocol="oidc", **kwargs):
        from skeleton.tenancy.models import Organization

        super().__init__(*args, **kwargs)
        self.fields["protocol"].initial = protocol
        field = self.fields["bound_organization"]
        field.queryset = Organization.objects.filter(is_active=True).order_by("name")
        field.label_from_instance = lambda organization: f"{organization.name} ({organization.slug})"
        hidden = {
            "oidc": ("entity_id", "sp_entity_id", "private_key", *_SamlOptions.base_fields),
            "saml": ("issuer", "secret"),
        }
        for name in hidden[protocol]:
            self.fields.pop(name, None)

    def service_data(self):
        data = dict(self.cleaned_data)
        protocol = data["protocol"]
        values = {
            name: data[name]
            for name in (
                "protocol",
                "provider_id",
                "name",
                "bound_organization",
                "client_id",
                "show_on_login",
                "trust_verified_email",
            )
        }
        if protocol == "oidc":
            values.update(issuer=data["issuer"], secret=data["secret"])
        else:
            values.update(
                entity_id=data["entity_id"],
                sp_entity_id=data["sp_entity_id"],
                private_key=data["private_key"],
                settings=self.saml_settings(),
            )
        return values


class IdentityProviderEditForm(_SamlOptions):
    name = forms.CharField(label="Nome exibido", max_length=200)
    enabled = forms.BooleanField(label="Ativo", required=False, help_text="Desligado, ninguém entra por ele.")
    show_on_login = forms.BooleanField(label="Mostrar na tela de login", required=False)
    trust_verified_email = forms.BooleanField(label="Confiar no email verificado pelo provedor", required=False)
    secret = _secret("Novo segredo do cliente", "Em branco mantém o atual.")
    private_key = _secret("Nova chave privada do SP", "Em branco mantém a atual.")
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(self, *args, protocol="oidc", **kwargs):
        super().__init__(*args, **kwargs)
        drop = ("private_key", *_SamlOptions.base_fields) if protocol == "oidc" else ("secret",)
        for name in drop:
            self.fields.pop(name, None)
        self.protocol = protocol

    def service_data(self):
        data = dict(self.cleaned_data)
        values = {name: data[name] for name in ("name", "enabled", "show_on_login", "trust_verified_email")}
        for secret in ("secret", "private_key"):
            if data.get(secret):
                values[secret] = data[secret]
        if self.protocol == "saml":
            values["settings"] = self.saml_settings()
        return values


def _row(sysid):
    from skeleton.accounts.models import IdentityProvider

    row = IdentityProvider.objects.select_related("bound_organization").filter(pk=sysid).first()
    if row is None:
        raise Http404
    return row


def _edit_initial(row):
    idp = (row.settings or {}).get("idp", {})
    sp = (row.settings or {}).get("sp", {})
    return {
        "name": row.name,
        "enabled": row.enabled,
        "show_on_login": row.show_on_login,
        "trust_verified_email": row.trust_verified_email,
        "expected_revision": row.revision,
        **{f"idp_{key}": idp.get(key, "") for key in ("sso_url", "slo_url", "metadata_url", "x509cert")},
        "sp_x509cert": sp.get("x509cert", ""),
    }


@platform_page
@require_safe
def identity_providers(request):
    from skeleton.accounts.models import IdentityProvider

    rows = IdentityProvider.objects.select_related("bound_organization").order_by("provider_id")
    return render(
        request,
        "skeleton/web/platform/identity_providers.html",
        {"platform_section": "identity-providers", "rows": rows},
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def identity_provider_create(request):
    from skeleton.accounts.identity_providers import create_identity_provider

    protocol = request.GET.get("protocol") or request.POST.get("protocol") or "oidc"
    if protocol not in ("oidc", "saml"):
        raise Http404
    form = IdentityProviderCreateForm(request.POST if request.method == "POST" else None, protocol=protocol)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    row = create_identity_provider(actor=request.user, data=form.service_data())
            except ValidationError as exc:
                status = form_error(form, exc)
            else:
                messages.success(request, f"Provedor {row.provider_id} cadastrado.")
                return redirect("platform:identity-provider", sysid=row.pk)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/platform/form.html",
        {
            "platform_section": "identity-providers",
            "form": form,
            "title": "Novo provedor " + ("OIDC" if protocol == "oidc" else "SAML"),
            "eyebrow": "Provedores de identidade",
            "description": "A identidade (provider_id, organização, issuer ou entity ID e client ID) não muda depois. "
            "Segredos são guardados cifrados e não aparecem de novo.",
            "submit_label": "Cadastrar provedor",
            "cancel_url": reverse("platform:identity-providers"),
        },
        status=status,
    )


def _detail_page(request, row, form, *, status=200):
    return render(
        request,
        "skeleton/web/platform/identity_provider.html",
        {"platform_section": "identity-providers", "row": row, "form": form},
        status=status,
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def identity_provider_detail(request, sysid):
    from skeleton.accounts.identity_providers import update_identity_provider

    row = _row(sysid)
    if request.method != "POST":
        return _detail_page(request, row, IdentityProviderEditForm(initial=_edit_initial(row), protocol=row.protocol))
    form = IdentityProviderEditForm(request.POST, protocol=row.protocol)
    if IMMUTABLE_POST_FIELDS & set(request.POST):
        form.is_valid()
        form.add_error(None, "A identidade do provedor não pode ser alterada; cadastre outro provedor.")
        return _detail_page(request, row, form, status=400)
    if not form.is_valid():
        return _detail_page(request, row, form, status=400)
    try:
        with transaction.atomic():
            row = update_identity_provider(
                actor=request.user,
                provider_id=row.pk,
                data=form.service_data(),
                expected_revision=form.cleaned_data["expected_revision"],
            )
    except (ValidationError, ConcurrentUpdate) as exc:
        status = form_error(form, exc)
        return _detail_page(request, _row(sysid), form, status=status)
    messages.success(request, "Provedor atualizado.")
    return redirect("platform:identity-provider", sysid=row.pk)


@platform_page
@require_POST
@transaction.atomic
def identity_provider_check(request, sysid):
    from skeleton.accounts.identity_providers import check_identity_provider

    row = _row(sysid)
    code = check_identity_provider(actor=request.user, provider_id=row.pk)
    if code == "ok":
        messages.success(request, "Teste concluído: o provedor respondeu como esperado.")
    else:
        messages.warning(request, f"Teste sem sucesso: {code}.")
    return redirect(reverse("platform:identity-provider", kwargs={"sysid": row.pk}))
