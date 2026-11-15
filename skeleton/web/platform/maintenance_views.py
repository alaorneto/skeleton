"""Administrative maintenance at /platform/maintenance/ (#447): enter and leave the mode.

Only the full platform administrator operates it (``platform_page`` without a delegated
capability), with MFA, a recent authentication and CSRF on every change. The state lives in the
instance SQLite (``skeleton.instance.maintenance``), so the page keeps working during the mode it
controls. The message never reaches a log, a security event or the change log.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.logging import security_event
from skeleton.instance.maintenance import (
    MESSAGE_MAX_LENGTH,
    MaintenanceError,
    clean_ends_at,
    clean_message,
    enter,
    leave,
    read_state,
    store_path,
)
from skeleton.instance.store import InstanceStore, InstanceStoreError

from .guards import platform_page


class EnterMaintenanceForm(forms.Form):
    message = forms.CharField(
        label=gettext_lazy("Mensagem"),
        required=False,
        strip=False,
        max_length=MESSAGE_MAX_LENGTH,
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": MESSAGE_MAX_LENGTH}),
        help_text=gettext_lazy("Texto simples mostrado a todos. Sem mensagem, vale o texto padrão de manutenção."),
    )
    ends_at = forms.DateTimeField(
        label=gettext_lazy("Término agendado"),
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        help_text=gettext_lazy("Opcional. No horário marcado o modo termina sozinho; sem ele, dura até alguém sair."),
    )
    pause_jobs = forms.BooleanField(
        label=gettext_lazy("Pausar jobs (worker e scheduler) durante a manutenção"), required=False, initial=True
    )
    confirm = forms.BooleanField(
        label=gettext_lazy("Confirmo: o site e a API respondem 503 para todos, menos administradores da plataforma"),
        required=True,
        error_messages={"required": gettext_lazy("Marque a confirmação para entrar em manutenção.")},
    )

    def clean_message(self):
        try:
            return clean_message(self.cleaned_data.get("message"))
        except MaintenanceError as error:
            raise forms.ValidationError(str(error)) from None

    def clean_ends_at(self):
        try:
            return clean_ends_at(self.cleaned_data.get("ends_at"))
        except MaintenanceError as error:
            raise forms.ValidationError(str(error)) from None


def _store() -> InstanceStore | None:
    path = store_path()
    return InstanceStore(path) if path else None


def _page(request, *, form=None, status=200):
    store = _store()
    state = None
    unreadable = False
    if store is not None:
        try:
            state = read_state(store)
        except InstanceStoreError:
            unreadable = True
    now = timezone.now()
    return render(
        request,
        "skeleton/web/platform/maintenance.html",
        {
            "platform_section": "maintenance",
            "state": state,
            "active": bool(state and state.is_active(now)),
            "expired": bool(state and state.expired(now)),
            "unreadable": unreadable or store is None,
            "form": form or EnterMaintenanceForm(),
            "time_zone": timezone.get_current_timezone_name(),
        },
        status=status,
    )


@never_cache
@platform_page
@require_http_methods(["GET", "HEAD"])
def maintenance(request):
    return _page(request)


@never_cache
@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
def maintenance_enter(request):
    form = EnterMaintenanceForm(request.POST)
    if not form.is_valid():
        return _page(request, form=form, status=400)
    store = _store()
    if store is None:
        return _page(request, form=form, status=409)
    source = f"platform:{request.user.pk}"
    data = form.cleaned_data
    try:
        enter(store, message=data["message"], ends_at=data["ends_at"], pause_jobs=data["pause_jobs"], source=source)
    except MaintenanceError as error:  # The end passed between the form and the write.
        form.add_error("ends_at", str(error))
        return _page(request, form=form, status=400)
    except InstanceStoreError:
        form.add_error(None, gettext("O SQLite da instância não pôde ser gravado. Use a CLI no servidor."))
        return _page(request, form=form, status=503)
    security_event("platform.maintenance_entered", reason_code="platform", source=source)
    messages.warning(request, gettext("Manutenção ativa. O site e a API respondem 503 para todos os outros."))
    return redirect("platform:maintenance")


@never_cache
@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
def maintenance_exit(request):
    store = _store()
    source = f"platform:{request.user.pk}"
    try:
        left = store is not None and leave(store, source=source)
    except InstanceStoreError:
        messages.error(request, gettext("O SQLite da instância não pôde ser gravado. Use a CLI no servidor."))
        return redirect("platform:maintenance")
    if left:
        security_event("platform.maintenance_exited", reason_code="platform", source=source)
        messages.success(request, gettext("Manutenção encerrada. O site reabre em até 5 segundos."))
    else:
        messages.info(request, gettext("A manutenção já estava inativa."))
    return redirect("platform:maintenance")
