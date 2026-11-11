"""Instance configuration at /platform/instance/ (#147): explicit views over the catalog.

Lists every catalog key with its effective origin (environment, instance SQLite, default) and
lets the platform administrator change the keys that the environment does not override.
Secrets are never rendered: the page shows only whether one is set. Every change needs a
recent authentication and is recorded in the store's change log and as a security event,
always without the value. Changes take effect after a restart.
"""

from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.logging import security_event
from skeleton.instance.resolve import ORIGIN_ENV, resolve_instance_settings
from skeleton.instance.schema import CATALOG
from skeleton.instance.writer import clear_setting, write_setting

from .guards import platform_page

MASK = "••••••••"


class SettingForm(forms.Form):
    key = forms.ChoiceField(choices=[(key, key) for key in CATALOG])
    action = forms.ChoiceField(choices=(("set", "Gravar"), ("clear", "Voltar ao padrão")))
    value = forms.CharField(required=False, max_length=2000, strip=False)


def _rows():
    rows = []
    for item in resolve_instance_settings(settings.BASE_DIR):
        spec = CATALOG[item.key]
        if item.origin == "default":
            shown = ""
        elif item.secret:
            shown = MASK
        else:
            shown = item.value or ""
        rows.append(
            {
                "key": item.key,
                "env": item.env,
                "description": spec.description,
                "kind": spec.kind,
                "secret": spec.secret,
                "origin": item.origin,
                "value": shown,
                "editable": item.origin != ORIGIN_ENV,
            }
        )
    return rows


def _page(request, *, form=None, status=200):
    return render(
        request,
        "skeleton/web/platform/instance.html",
        {"platform_section": "instance", "rows": _rows(), "form": form, "mask": MASK},
        status=status,
    )


def _validate(key: str, value: str) -> None:
    """Catalog validation, plus the setup's privilege check for the runtime database."""
    CATALOG[key].clean(value)
    if key == "database_url":
        from skeleton.setup.database import DatabaseCheckError, validate_runtime

        try:
            validate_runtime(value)
        except DatabaseCheckError as error:
            raise ValueError(str(error)) from None


@platform_page
@require_http_methods(["GET", "HEAD"])
def instance(request):
    return _page(request)


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
def instance_setting(request):
    form = SettingForm(request.POST)
    if set(request.POST) - {"key", "action", "value", "csrfmiddlewaretoken"} or not form.is_valid():
        return _page(request, form=form, status=400)
    key, action, value = form.cleaned_data["key"], form.cleaned_data["action"], form.cleaned_data["value"]
    origin = next(item.origin for item in resolve_instance_settings(settings.BASE_DIR) if item.key == key)
    if origin == ORIGIN_ENV:
        form.add_error("key", "Esta chave vem do ambiente e não pode ser alterada aqui.")
        return _page(request, form=form, status=409)
    source = f"platform:{request.user.pk}"
    try:
        if action == "clear":
            changed = clear_setting(settings.BASE_DIR, key, source=source)
        else:
            if CATALOG[key].secret and value == "":
                form.add_error("value", "Informe o novo valor; o atual nunca é exibido.")
                return _page(request, form=form, status=400)
            _validate(key, value)
            write_setting(settings.BASE_DIR, key, value, source=source)
            changed = True
    except ValueError as error:
        form.add_error("value", str(error))
        return _page(request, form=form, status=400)
    if changed:
        security_event("instance.setting_changed", reason_code=f"{action}:{key}", source=source)
        messages.success(request, f"{key} atualizada. A mudança vale depois do reinício da instância.")
    return redirect("platform:instance")


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
def instance_restart(request):
    from skeleton.setup.restart import request_restart

    how = request_restart()
    security_event("instance.restart_requested", reason_code=how, source=f"platform:{request.user.pk}")
    if how == "manual":
        messages.warning(request, "Sem supervisor conhecido: reinicie o serviço para aplicar as mudanças.")
    else:
        messages.success(request, "Reinício solicitado. A instância volta em alguns segundos.")
    return redirect("platform:instance")
