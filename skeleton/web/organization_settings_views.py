"""``/o/<org>/settings/``: the administrator edits the organization's defaults and brand (#167)."""

from __future__ import annotations

from zoneinfo import available_timezones

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.organization_settings import WRITE_FIELDS, current_settings, revision_of, update_settings

from .views import tenant_page


def _timezone_choices() -> list[tuple[str, str]]:
    return [("", "Padrão da instância")] + [(zone, zone) for zone in sorted(available_timezones())]


class OrganizationSettingsForm(forms.Form):
    display_name = forms.CharField(
        label="Nome de exibição",
        max_length=200,
        required=False,
        help_text="Aparece no menu e nos emails. Em branco, vale o nome da organização.",
    )
    default_language = forms.ChoiceField(
        label="Idioma padrão", required=False, help_text="Vale para quem não escolheu um idioma no perfil."
    )
    default_timezone = forms.ChoiceField(
        label="Fuso horário padrão", required=False, help_text="Vale para quem não escolheu um fuso no perfil."
    )
    brand_color = forms.CharField(
        label="Cor da marca",
        max_length=7,
        required=False,
        help_text="Formato #RRGGBB. Em branco, vale a cor padrão.",
        widget=forms.TextInput(attrs={"placeholder": "#1d4ed8", "autocomplete": "off"}),
    )
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_language"].choices = [("", "Padrão da instância"), *settings.LANGUAGES]
        self.fields["default_timezone"].choices = _timezone_choices()


def _apply_error(form: OrganizationSettingsForm, error: ValidationError) -> None:
    for field, problems in error.message_dict.items():
        form.add_error(field if field in form.fields else None, problems)


@tenant_page(admin=True)
@never_cache
@require_http_methods(["GET", "POST"])
def organization_settings(request, organization_id):
    record = current_settings()
    initial = {name: getattr(record, name, "") for name in WRITE_FIELDS}
    form = OrganizationSettingsForm(
        request.POST if request.method == "POST" else None, initial={**initial, "revision": revision_of(record)}
    )
    status = 200
    if request.method == "POST":
        if set(request.POST) - (set(form.fields) | {"csrfmiddlewaretoken"}):
            form.add_error(None, "Campo não permitido.")
            status = 400
        elif form.is_valid():
            data = {name: form.cleaned_data[name] for name in WRITE_FIELDS}
            try:
                with transaction.atomic():
                    update_settings(actor=request.user, data=data, expected_revision=form.cleaned_data["revision"])
            except ConcurrentUpdate:
                form.add_error(None, "As configurações mudaram. Atualize a página antes de tentar novamente.")
                status = 409
            except ValidationError as exc:
                _apply_error(form, exc)
                status = 400
            else:
                messages.success(request, "Configurações salvas.")
                return redirect("web:organization-settings", organization_id=organization_id)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/form.html",
        {
            "title": "Configurações da organização",
            "description": "Nome de exibição, idioma e fuso padrão e cor da marca.",
            "form": form,
            "organization_id": organization_id,
            "cancel_url": reverse("web:dashboard", kwargs={"organization_id": organization_id}),
            "breadcrumbs": [
                {"label": "Início", "url": reverse("web:dashboard", kwargs={"organization_id": organization_id})},
                {"label": "Configurações"},
            ],
        },
        status=status,
    )
