"""Instance configuration at /platform/instance/ (#147): explicit views over the catalog.

Lists every catalog key with its effective origin (environment, instance SQLite, default) and
lets the platform administrator change the keys that the environment does not override.
Secrets are never rendered: the page shows only whether one is set. Every change needs a
recent authentication and is recorded in the store's change log and as a security event,
always without the value. Changes take effect after a restart. In cluster mode (#673) the page
is read-only: the configuration comes from the environment of every node, and both writes answer
409 ``instance_cluster_mode``.
"""

from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.logging import security_event
from skeleton.instance.resolve import ORIGIN_ENV, is_cluster, resolve_instance_settings
from skeleton.instance.schema import CATALOG, SETTING_GROUPS
from skeleton.instance.writer import can_clear, clear_setting, write_setting
from skeleton.web.confirm import PLATFORM_TEMPLATE, Confirmation, confirmation_step

from .guards import platform_page

MASK = "••••••••"


class SettingForm(forms.Form):
    key = forms.ChoiceField(choices=[(key, key) for key in CATALOG])
    action = forms.ChoiceField(choices=(("set", gettext_lazy("Gravar")), ("clear", gettext_lazy("Voltar ao padrão"))))
    value = forms.CharField(required=False, max_length=2000, strip=False)


ORIGINS = ("env", "instance", "default")


def _rows(cluster: bool, form=None):
    """One row per catalog key; the key of a refused POST carries its error and stays open (#779)."""
    failed = form.data.get("key") if form is not None and form.errors else None
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
                "editable": not cluster and item.origin != ORIGIN_ENV,
                "clearable": not cluster and item.origin == "instance" and can_clear(spec),
                "group": spec.group,
                "error": " ".join(str(e) for errors in form.errors.values() for e in errors)
                if failed == item.key
                else "",
            }
        )
    return rows


def _sections(rows, *, query: str, origin: str):
    """The rows by section of the catalog, filtered by text and origin on the server (#779)."""
    needle = query.casefold()
    shown = [
        row
        for row in rows
        if (not origin or row["origin"] == origin)
        and (not needle or any(needle in str(row[field]).casefold() for field in ("key", "env", "description")))
    ]
    return [
        {"id": group, "label": label, "rows": [row for row in shown if row["group"] == group]}
        for group, label, _ in SETTING_GROUPS
        if any(row["group"] == group for row in shown)
    ]


class IdentityForm(forms.Form):
    """Name and purpose of this instance (#708); the identifier itself never changes."""

    name = forms.CharField(label=gettext_lazy("Nome"), max_length=200, required=False)
    purpose = forms.CharField(
        label=gettext_lazy("Finalidade"), max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


def _page(request, *, form=None, identity_form=None, status=200):
    from skeleton.system.instance_identity import instance_identity

    cluster = is_cluster()
    identity = instance_identity()
    identity_form = identity_form or IdentityForm(
        initial={"name": identity.name, "purpose": identity.purpose, "expected_revision": identity.revision}
    )
    query = request.GET.get("q", "").strip()[:100]
    origin = request.GET.get("origem", "")
    origin = origin if origin in ORIGINS else ""
    rows = _rows(cluster, form)
    return render(
        request,
        "skeleton/web/platform/instance.html",
        {
            "platform_section": "instance",
            "rows": rows,
            "sections": _sections(rows, query=query, origin=origin),
            "query": query,
            "origin": origin,
            "origins": ORIGINS,
            "filtered": bool(query or origin),
            "form": form,
            "mask": MASK,
            "cluster": cluster,
            "identity": identity,
            "identity_form": identity_form,
        },
        status=status,
    )


def _cluster_refusal() -> HttpResponse:
    return HttpResponse(
        gettext("instance_cluster_mode: no modo cluster a configuração vem do ambiente de cada nó."),
        status=409,
        content_type="text/plain; charset=utf-8",
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
    if is_cluster():
        return _cluster_refusal()
    form = SettingForm(request.POST)
    if set(request.POST) - {"key", "action", "value", "csrfmiddlewaretoken"} or not form.is_valid():
        return _page(request, form=form, status=400)
    key, action, value = form.cleaned_data["key"], form.cleaned_data["action"], form.cleaned_data["value"]
    origin = next(item.origin for item in resolve_instance_settings(settings.BASE_DIR) if item.key == key)
    if origin == ORIGIN_ENV:
        form.add_error("key", gettext("Esta chave vem do ambiente e não pode ser alterada aqui."))
        return _page(request, form=form, status=409)
    if action == "clear" and not can_clear(CATALOG[key]):
        form.add_error("key", gettext("Esta chave é obrigatória: grave um novo valor em vez de voltar ao padrão."))
        return _page(request, form=form, status=409)
    source = f"platform:{request.user.pk}"
    try:
        if action == "clear":
            changed = clear_setting(settings.BASE_DIR, key, source=source)
        else:
            if CATALOG[key].secret and value == "":
                form.add_error("value", gettext("Informe o novo valor; o atual nunca é exibido."))
                return _page(request, form=form, status=400)
            _validate(key, value)
            write_setting(settings.BASE_DIR, key, value, source=source)
            changed = True
    except ValueError as error:
        form.add_error("value", str(error))
        return _page(request, form=form, status=400)
    if changed:
        security_event("instance.setting_changed", reason_code=f"{action}:{key}", source=source)
        messages.success(
            request, gettext("%(key)s atualizada. A mudança vale depois do reinício da instância.") % {"key": key}
        )
    return redirect("platform:instance")


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
def instance_identity_update(request):
    """Rename the instance or describe its purpose, with CAS (#708)."""
    from django.core.exceptions import ValidationError

    from skeleton.common.exceptions import ConcurrentUpdate
    from skeleton.system.instance_identity import update_instance_identity

    form = IdentityForm(request.POST)
    if not form.is_valid():
        return _page(request, identity_form=form, status=400)
    try:
        update_instance_identity(
            actor=request.user,
            name=form.cleaned_data["name"],
            purpose=form.cleaned_data["purpose"],
            expected_revision=form.cleaned_data["expected_revision"],
        )
    except ConcurrentUpdate:
        form.add_error(None, gettext("A identidade mudou. Atualize a página."))
        return _page(request, identity_form=form, status=409)
    except ValidationError as exc:
        for field, problems in exc.message_dict.items():
            form.add_error(field if field in form.fields else None, problems)
        return _page(request, identity_form=form, status=400)
    messages.success(request, gettext("Identidade da instância atualizada."))
    return redirect("platform:instance")


def _describe_restart(request):
    return Confirmation(
        title=gettext("Reiniciar a instância"),
        message=gettext(
            "A instância para por alguns segundos e as mudanças de configuração passam a valer. Quem estiver "
            "usando perde a requisição em andamento e precisa recarregar a página."
        ),
        submit_label=gettext("Reiniciar agora"),
        cancel_url=reverse("platform:instance"),
        eyebrow=gettext("Configuração da instância"),
        template=PLATFORM_TEMPLATE,
        context={"platform_section": "instance"},
    )


@platform_page
@confirmation_step(_describe_restart)
@require_http_methods(["POST"])
@recent_authentication_required
def instance_restart(request):
    from skeleton.setup.restart import request_restart

    if is_cluster():
        return _cluster_refusal()
    how = request_restart()
    security_event("instance.restart_requested", reason_code=how, source=f"platform:{request.user.pk}")
    if how == "manual":
        messages.warning(request, gettext("Sem supervisor conhecido: reinicie o serviço para aplicar as mudanças."))
    else:
        messages.success(request, gettext("Reinício solicitado. A instância volta em alguns segundos."))
    return redirect("platform:instance")
