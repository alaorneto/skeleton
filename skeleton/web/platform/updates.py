"""Versions and updates of the platform (/platform/updates/), in steps.

The web process only asks the independent deployment agent for previews and
approvals; it never runs pip, migrations or DDL. Mutations require a platform
administrator, CSRF and a recent authentication. The upload handler is bounded
and installed before CSRF parses the multipart body.
"""

import re
import tempfile
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods, require_safe

from skeleton.accounts.reauthentication import recent_authentication_required, require_recent_authentication
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system.models import ComponentInstallation, DeploymentOperation
from skeleton.system.releases import (
    DeploymentUnavailable,
    agent_client,
    confirm_operation,
    create_preview,
    reconcile_operation,
    require_release_operator,
    retry_submission,
)

from .guards import platform_page

__all__ = [
    "add_dependency",
    "available",
    "fetch_release",
    "installed",
    "operation",
    "operations",
    "plan",
    "recent_authentication",
    "upload",
]

STEPS = (
    ("installed", gettext_lazy("Versões instaladas"), "platform:updates"),
    ("available", gettext_lazy("Novas versões"), "platform:updates-available"),
    ("upload", gettext_lazy("Enviar pacote"), "platform:updates-upload"),
    ("plan", gettext_lazy("Preparar plano"), "platform:updates-plan"),
    ("operations", gettext_lazy("Operações"), "platform:updates-operations"),
)


class UploadForm(forms.Form):
    package = forms.FileField(
        label=gettext_lazy("Pacote assinado (.skapp)"),
        help_text=gettext_lazy(
            "Assinado por um publicador autorizado. O agente verifica assinatura e conteúdo antes de liberar o digest."
        ),
    )


class PlanForm(forms.Form):
    action = forms.ChoiceField(
        label=gettext_lazy("Operação"),
        choices=(
            ("install", gettext_lazy("Instalar")),
            ("update", gettext_lazy("Atualizar")),
            ("uninstall", gettext_lazy("Desinstalar código")),
            ("reinstall", gettext_lazy("Reinstalar código")),
            ("core_update", gettext_lazy("Atualizar núcleo")),
        ),
    )
    artifacts = forms.CharField(
        label=gettext_lazy("Digests dos pacotes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=gettext_lazy(
            "SHA-256 devolvidos pelo envio ou pela busca de versões; separe por espaço ou vírgula para atualizar em conjunto."
        ),
    )
    app_ids = forms.CharField(
        label=gettext_lazy("IDs das apps a desinstalar"),
        required=False,
        help_text=gettext_lazy("Somente para desinstalação. Dados e migrations históricas são preservados."),
    )

    def clean_artifacts(self):
        values = self.cleaned_data["artifacts"].replace(",", " ").split()
        if len(values) > 32 or any(not re.fullmatch("[0-9a-f]{64}", value) for value in values):
            raise forms.ValidationError(gettext("Use digests SHA-256 retornados pelo upload."))
        return list(dict.fromkeys(values))

    def clean_app_ids(self):
        values = self.cleaned_data["app_ids"].replace(",", " ").split()
        if len(values) > 32 or any(not re.fullmatch("[a-zA-Z0-9_.-]{1,100}", value) for value in values):
            raise forms.ValidationError(gettext("ID de app inválido."))
        return list(dict.fromkeys(values))

    def clean(self):
        values = super().clean()
        if values.get("action") == "uninstall":
            if values.get("artifacts") or not values.get("app_ids"):
                raise forms.ValidationError(gettext("Desinstalação exige IDs de apps, sem pacotes."))
        elif not values.get("artifacts") or values.get("app_ids"):
            raise forms.ValidationError(gettext("Instalação/atualização exige pacotes, sem IDs de retirada."))
        return values


class ConfirmationForm(forms.Form):
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    plan_digest = forms.CharField(widget=forms.HiddenInput)


class BoundedPackageUpload(FileUploadHandler):
    def __init__(self, request):
        super().__init__(request)
        self.total = 0
        self.exceeded = False

    def receive_data_chunk(self, raw_data, start):
        self.total += len(raw_data)
        if self.total > getattr(settings, "SKELETON_PACKAGE_MAX_BYTES", 128 * 1024 * 1024):
            self.exceeded = True
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None


def recent_authentication(request):
    require_recent_authentication(request)


def _context(step, **values):
    return {"platform_section": "updates", "updates_step": step, "steps": STEPS, **values}


def _error(request, exc):
    if isinstance(exc, DeploymentUnavailable):
        messages.error(
            request, gettext("O agente de deployment não está disponível ou não foi configurado nesta instalação.")
        )
        return
    messages.error(
        request, gettext("Operação indisponível: %(value1)s.") % {"value1": getattr(exc, "code", type(exc).__name__)}
    )


PROBLEM_TEXT = {
    "missing_dependency": gettext_lazy(
        "{component} exige {dependency} {required}, que não está instalada nem incluída no plano."
    ),
    "incompatible_dependency_version": gettext_lazy(
        "{component} exige {dependency} {required}; a versão resultante seria {found}."
    ),
    "dependency_cycle": gettext_lazy("Dependência circular: {path}."),
    "conflicting_release": gettext_lazy(
        "{component} declara conflito com {dependency} {required}; a versão resultante seria {found}."
    ),
    "incompatible_platform": "{component} exige {dependency} {required}; o ambiente-alvo tem {found}.",
    "identity_collision": gettext_lazy("{component} aparece mais de uma vez no plano."),
    "namespace_collision": gettext_lazy("{component} usa um label ou pacote Python já ocupado por outra app."),
    "wheel_pin_conflict": "{component} fixa {dependency} {required}, em conflito com {found} fixado por outro componente.",
    "installed_dependents": gettext_lazy(
        "{component} depende de {dependency}: retire as duas no mesmo plano ou mantenha {dependency}."
    ),
}


class _Missing(dict):
    def __missing__(self, key):
        return "—"


def describe_problems(details):
    """Operator-facing text for the agent's closed problem shape; values stay autoescaped."""
    problems = []
    for detail in details:
        values = _Missing(detail)
        values["path"] = " → ".join(detail.get("path", ())) or "—"
        text = PROBLEM_TEXT.get(detail["code"], gettext("Problema de dependência: {code} em {component}."))
        problems.append(
            {
                "text": text.format_map(values),
                "suggestions": detail.get("suggestions", ()),
                # An out-of-range installed dependency is replaced by an update, not installed.
                "upgrade": detail["code"] == "incompatible_dependency_version",
            }
        )
    return problems


def _installed_components():
    inventory = agent_client().inventory()
    return {
        entry["manifest"]["app_id"] for entry in inventory.get("apps", []) if entry.get("state") != "schema_retained"
    }


def _plan_page(request, form, *, status=200, artifact=None, problems=(), plan_action="", plan_artifacts=""):
    return render(
        request,
        "skeleton/web/platform/updates/plan.html",
        _context(
            "plan",
            plan_form=form,
            artifact=artifact,
            problems=problems,
            plan_action=plan_action,
            plan_artifacts=plan_artifacts,
        ),
        status=status,
    )


# --------------------------------------------------------------------------- steps


@platform_page(capability="updates.operate", writes=True)
@require_safe
def installed(request):
    from skeleton.versioning import core_version

    inventory = None
    try:
        inventory = agent_client().inventory()
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    return render(
        request,
        "skeleton/web/platform/updates/installed.html",
        _context(
            "installed",
            core=core_version(),
            inventory=inventory,
            components=ComponentInstallation.objects.order_by("kind", "component_id"),
        ),
    )


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["GET", "POST"])
def available(request):
    updates = None
    if request.method == "POST":
        try:
            updates = agent_client().check_updates()
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
    return render(request, "skeleton/web/platform/updates/available.html", _context("available", updates=updates))


@csrf_exempt
@platform_page(capability="updates.operate", writes=True)
def upload(request):
    # Install the bounded streaming handler before CSRF parses multipart data.
    request.upload_handlers.insert(0, BoundedPackageUpload(request))
    return _upload(request)


@csrf_protect
@recent_authentication_required(allow_get=True)
def _upload(request):
    require_release_operator(request.user)
    if request.method != "POST":
        return render(
            request, "skeleton/web/platform/updates/upload.html", _context("upload", upload_form=UploadForm())
        )
    recent_authentication(request)
    handler = request.upload_handlers[0]
    form = UploadForm(request.POST, request.FILES)
    if handler.exceeded:
        return HttpResponse(gettext("Pacote excede o limite de upload."), status=413)
    if form.is_valid():
        try:
            uploaded = form.cleaned_data["package"]
            with tempfile.TemporaryDirectory(prefix="skeleton-upload-") as directory:
                source = Path(directory) / "upload.skapp"
                with source.open("xb") as stream:
                    for chunk in uploaded.chunks():
                        stream.write(chunk)
                source.chmod(0o600)
                artifact = agent_client().upload(source)
            messages.success(request, gettext("Pacote verificado. Confira o plano e visualize o impacto."))
            return _plan_page(
                request,
                PlanForm(initial={"artifacts": artifact["digest"], "action": "install"}),
                artifact=artifact,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
    return render(
        request, "skeleton/web/platform/updates/upload.html", _context("upload", upload_form=form), status=400
    )


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def plan(request):
    if request.method != "POST":
        return _plan_page(request, PlanForm())
    recent_authentication(request)
    form = PlanForm(request.POST)
    if form.is_valid():
        try:
            row = create_preview(user=request.user, **form.cleaned_data)
            return redirect("platform:updates-operation", operation_id=row.pk)
        except (ValueError, RuntimeError, OSError) as exc:
            return _preview_failure(request, form, exc)
    return _plan_page(request, form, status=400)


def _preview_failure(request, form, exc):
    _error(request, exc)
    cleaned = getattr(form, "cleaned_data", {}) or {}
    return _plan_page(
        request,
        form,
        status=409 if getattr(exc, "details", ()) else 400,
        problems=describe_problems(getattr(exc, "details", ())),
        plan_action=cleaned.get("action", ""),
        plan_artifacts=" ".join(cleaned.get("artifacts", ())),
    )


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["POST"])
@recent_authentication_required(allow_get=True)
def add_dependency(request):
    """Fetch an operator-chosen catalogue suggestion and preview the plan again with it."""
    recent_authentication(request)
    identifiers = {name: request.POST.get(name, "") for name in ("component_id", "version", "source_id")}
    if any(not value or len(value) > 128 for value in identifiers.values()):
        return HttpResponse(gettext("Identificação de release inválida."), status=400)
    form = PlanForm({"action": request.POST.get("action", ""), "artifacts": request.POST.get("artifacts", "")})
    if not form.is_valid() or form.cleaned_data["action"] in {"uninstall", "reinstall"}:
        return HttpResponse(gettext("Plano de origem inválido."), status=400)
    try:
        artifact = agent_client().fetch_release(**identifiers)
        action = "update" if request.POST.get("upgrade") == "1" else form.cleaned_data["action"]
        artifacts = list(dict.fromkeys([*form.cleaned_data["artifacts"], artifact["digest"]]))
        form = PlanForm({"action": action, "artifacts": " ".join(artifacts)})
        form.is_valid()
        row = create_preview(user=request.user, action=action, artifacts=artifacts)
        return redirect("platform:updates-operation", operation_id=row.pk)
    except (ValueError, RuntimeError, OSError) as exc:
        return _preview_failure(request, form, exc)


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["POST"])
@recent_authentication_required(allow_get=True)
def fetch_release(request):
    recent_authentication(request)
    try:
        identifiers = {name: request.POST.get(name, "") for name in ("component_id", "version", "source_id")}
        if any(not value or len(value) > 128 for value in identifiers.values()):
            return HttpResponse(gettext("Identificação de release inválida."), status=400)
        artifact = agent_client().fetch_release(**identifiers)
        if identifiers["component_id"] == "django-skeleton":
            action = "core_update"
        else:
            action = "update" if identifiers["component_id"] in _installed_components() else "install"
        messages.success(request, gettext("Versão obtida e verificada. Confira o plano e visualize o impacto."))
        return _plan_page(
            request, PlanForm(initial={"action": action, "artifacts": artifact["digest"]}), artifact=artifact
        )
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
        return redirect("platform:updates-available")


@platform_page(capability="updates.operate", writes=True)
@require_safe
def operations(request):
    return render(
        request,
        "skeleton/web/platform/updates/operations.html",
        _context("operations", operations=DeploymentOperation.objects.order_by("-created_at")[:50]),
    )


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def operation(request, operation_id):
    row = get_object_or_404(DeploymentOperation.objects.select_for_update(), pk=operation_id)
    if request.method == "POST":
        recent_authentication(request)
        form = ConfirmationForm(request.POST)
        if not form.is_valid() or form.cleaned_data["plan_digest"] != row.plan_digest:
            return HttpResponse(gettext("Confirmação do plano inválida."), status=400)
        try:
            if request.POST.get("intent") == "retry":
                retry_submission(user=request.user, operation_id=row.pk, revision=form.cleaned_data["revision"])
            else:
                confirm_operation(user=request.user, operation_id=row.pk, revision=form.cleaned_data["revision"])
        except ConcurrentUpdate:
            return HttpResponse(gettext("A operação mudou; atualize o estado antes de confirmar."), status=409)
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
        return redirect("platform:updates-operation", operation_id=row.pk)
    status_url = None
    try:
        if row.state == "preview":
            status_url = agent_client().status_ticket(str(row.pk)).get("status_url")
        else:
            status = agent_client().operation(str(row.pk))
            reconcile_operation(row, status)
            status_url = status.get("status_url")
    except (ConcurrentUpdate, ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    return render(
        request,
        "skeleton/web/platform/updates/operation.html",
        _context(
            "operations",
            operation=row,
            confirmation_form=ConfirmationForm(initial={"revision": row.revision, "plan_digest": row.plan_digest}),
            status_url=status_url,
        ),
    )
