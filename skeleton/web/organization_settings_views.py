"""``/o/<org>/settings/``: the administrator edits the organization's defaults and brand (#167, #168).

The logo route serves the image inline only to members of the organization (the tenant
middleware answers 404 to anyone else), with private caching and ``nosniff``.
"""

from __future__ import annotations

from zoneinfo import available_timezones

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.branding import open_logo, remove_logo, replace_logo
from skeleton.tenancy.organization_settings import WRITE_FIELDS, current_settings, revision_of, update_settings

from .views import tenant_page


def _timezone_choices() -> list[tuple[str, str]]:
    return [("", gettext("Padrão da instância"))] + [(zone, zone) for zone in sorted(available_timezones())]


class OrganizationSettingsForm(forms.Form):
    display_name = forms.CharField(
        label=gettext_lazy("Nome de exibição"),
        max_length=200,
        required=False,
        help_text=gettext_lazy("Aparece no menu e nos emails. Em branco, vale o nome da organização."),
    )
    default_language = forms.ChoiceField(
        label=gettext_lazy("Idioma padrão"),
        required=False,
        help_text=gettext_lazy("Vale para quem não escolheu um idioma no perfil."),
    )
    default_timezone = forms.ChoiceField(
        label=gettext_lazy("Fuso horário padrão"),
        required=False,
        help_text=gettext_lazy("Vale para quem não escolheu um fuso no perfil."),
    )
    brand_color = forms.CharField(
        label=gettext_lazy("Cor da marca"),
        max_length=7,
        required=False,
        help_text=gettext_lazy(
            "Formato #RRGGBB, com contraste de pelo menos 4,5:1 com o branco. Em branco, vale a cor padrão."
        ),
        widget=forms.TextInput(attrs={"placeholder": "#1d4ed8", "autocomplete": "off"}),
    )
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_language"].choices = [("", gettext("Padrão da instância")), *settings.LANGUAGES]
        self.fields["default_timezone"].choices = _timezone_choices()


def _apply_error(form: OrganizationSettingsForm, error: ValidationError) -> None:
    for field, problems in error.message_dict.items():
        form.add_error(field if field in form.fields else None, problems)


def _page(request, organization_id, form, *, status=200, logo_errors=()):
    record = current_settings()
    dashboard = reverse("web:dashboard", kwargs={"organization_id": organization_id})
    return render(
        request,
        "skeleton/web/organization_settings.html",
        {
            "form": form,
            "organization_id": organization_id,
            "revision": revision_of(record),
            "has_logo": bool(record is not None and record.logo_key),
            "logo_errors": list(logo_errors),
            "cancel_url": dashboard,
            "breadcrumbs": [{"label": gettext("Início"), "url": dashboard}, {"label": gettext("Configurações")}],
        },
        status=status,
    )


def _form(request, record):
    initial = {name: getattr(record, name, "") for name in WRITE_FIELDS}
    return OrganizationSettingsForm(
        request.POST if request.method == "POST" else None, initial={**initial, "revision": revision_of(record)}
    )


@tenant_page(admin=True)
@never_cache
@require_http_methods(["GET", "POST"])
def organization_settings(request, organization_id):
    form = _form(request, current_settings())
    status = 200
    if request.method == "POST":
        if set(request.POST) - (set(form.fields) | {"csrfmiddlewaretoken"}):
            form.add_error(None, gettext("Campo não permitido."))
            status = 400
        elif form.is_valid():
            data = {name: form.cleaned_data[name] for name in WRITE_FIELDS}
            try:
                with transaction.atomic():
                    update_settings(actor=request.user, data=data, expected_revision=form.cleaned_data["revision"])
            except ConcurrentUpdate:
                form.add_error(None, gettext("As configurações mudaram. Atualize a página antes de tentar novamente."))
                status = 409
            except ValidationError as exc:
                _apply_error(form, exc)
                status = 400
            else:
                messages.success(request, gettext("Configurações salvas."))
                return redirect("web:organization-settings", organization_id=organization_id)
        else:
            status = 400
    return _page(request, organization_id, form, status=status)


@tenant_page(admin=True)
@never_cache
@require_POST
def organization_logo_update(request, organization_id):
    action = request.POST.get("action")
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = -1
    form = OrganizationSettingsForm(initial={"revision": revision_of(current_settings())})
    try:
        with transaction.atomic():
            if action == "upload" and "logo" in request.FILES:
                replace_logo(actor=request.user, upload=request.FILES["logo"], expected_revision=revision)
            elif action == "remove":
                remove_logo(actor=request.user, expected_revision=revision)
            else:
                raise ValidationError({"logo": gettext("Escolha um arquivo de imagem.")})
    except ConcurrentUpdate:
        return _page(
            request,
            organization_id,
            form,
            status=409,
            logo_errors=[gettext("As configurações mudaram. Atualize a página.")],
        )
    except ValidationError as exc:
        errors = [message for messages_ in exc.message_dict.values() for message in messages_]
        return _page(request, organization_id, form, status=400, logo_errors=errors)
    messages.success(request, gettext("Logo removido.") if action == "remove" else gettext("Logo atualizado."))
    return redirect("web:organization-settings", organization_id=organization_id)


@tenant_page()
@require_safe
def organization_logo(request, organization_id):
    from skeleton.files.storage import open_object

    found = open_logo()
    if found is None:
        raise Http404
    record, kind = found
    etag = '"' + record.logo_key.rsplit("/", 1)[1] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"}
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponseNotModified()
    else:
        # At most 512 KB: read inside the tenant context (streaming is refused there).
        with open_object(record.logo_key) as stream:
            response = HttpResponse(stream.read(), content_type=kind)
        response["Content-Disposition"] = "inline"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    for name, value in headers.items():
        response[name] = value
    return response
