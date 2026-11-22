"""Versions and updates of the platform (/platform/updates/), in steps.

The web process only asks the independent deployment agent for previews and
approvals; it never runs pip, migrations or DDL. Mutations require a platform
administrator, CSRF and a recent authentication. The upload handler is bounded
and installed before CSRF parses the multipart body, in place of the global
attachment bound (``SKELETON_PACKAGE_MAX_BYTES`` instead of ``SKELETON_UPLOAD_MAX_BYTES``).
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
from packaging.version import InvalidVersion, Version

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system.deployment_client import AgentError, sanitize_details
from skeleton.system.models import ComponentInstallation, DeploymentOperation
from skeleton.system.releases import (
    GITHUB_REPOSITORY,
    UPSTREAM_REPOSITORY,
    DeploymentUnavailable,
    agent_client,
    confirm_operation,
    invalidate_update_notice,
    plan_artifact,
    preview_status,
    reconcile_operation,
    record_preview,
    remove_update_source,
    request_preview,
    require_release_operator,
    retry_submission,
    save_update_source,
    submission_can_retry,
    update_sources,
)
from skeleton.system.releases import update_agent as request_agent_update

from .guards import platform_page

__all__ = [
    "add_dependency",
    "available",
    "fetch_release",
    "installed",
    "operation",
    "operations",
    "plan",
    "preview",
    "remove_source",
    "sources",
    "update_agent",
    "upload",
]

STEPS = (
    ("installed", gettext_lazy("Versões instaladas"), "platform:updates"),
    ("sources", gettext_lazy("Fontes"), "platform:updates-sources"),
    ("available", gettext_lazy("Novas versões"), "platform:updates-available"),
    ("upload", gettext_lazy("Enviar pacote"), "platform:updates-upload"),
    ("plan", gettext_lazy("Preparar plano"), "platform:updates-plan"),
    ("operations", gettext_lazy("Operações"), "platform:updates-operations"),
)


class UploadForm(forms.Form):
    package = forms.FileField(
        label=gettext_lazy("Pacote assinado (.skapp)"),
        help_text=gettext_lazy(
            "Pacote de componente (app ou núcleo) ou pacote de release, assinado por um publicador autorizado. "
            "O agente verifica assinatura e conteúdo antes de liberar o digest."
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


class SourceForm(forms.Form):
    """A GitHub repository the agent consults (#499); keys and tokens stay in the agent file."""

    source_id = forms.RegexField(r"^[a-z][a-z0-9-]{0,31}$", label=gettext_lazy("Identificador"), max_length=32)
    repository = forms.CharField(label=gettext_lazy("Repositório do GitHub (dono/nome)"), max_length=140)
    publisher = forms.ChoiceField(label=gettext_lazy("Publicador (chave aprovada no servidor)"), choices=())
    channel = forms.ChoiceField(label=gettext_lazy("Canal"), choices=(("stable", "stable"),), initial="stable")
    private = forms.BooleanField(
        label=gettext_lazy("Repositório privado (token configurado no servidor)"), required=False
    )

    def __init__(self, *args, publishers=(), private_available=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["publisher"].choices = [(publisher, publisher) for publisher in publishers]
        if not private_available:
            del self.fields["private"]

    def clean_repository(self):
        value = self.cleaned_data["repository"].strip()
        if "://" in value or value.lower().startswith(("github.com/", "www.github.com/")):
            raise forms.ValidationError(gettext("Informe só dono/nome, sem https://github.com/."))
        if not GITHUB_REPOSITORY.fullmatch(value):
            raise forms.ValidationError(gettext("Repositório inválido: use dono/nome."))
        return value


class ConfirmationForm(forms.Form):
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    plan_digest = forms.CharField(widget=forms.HiddenInput)
    force_window = forms.BooleanField(
        required=False,
        label=gettext_lazy("Forçar janela de manutenção"),
        help_text=gettext_lazy(
            "O plano pode trocar o release nó a nó, sem tirar o site do ar. Marque para parar todos os nós juntos."
        ),
    )

    def __init__(self, *args, rolling=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not rolling:
            del self.fields["force_window"]  # Only a rolling-eligible plan in a cluster offers the choice (#682).


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


def _context(step, **values):
    return {"platform_section": "updates", "updates_step": step, "steps": STEPS, **values}


# Operator-facing text of the agent update codes (#497); any other code is shown as is.
ERROR_TEXT = {
    "agent_update_required": gettext_lazy(
        "O agente de deployment é mais antigo que a linha do núcleo de destino: atualize o agente primeiro."
    ),
    "agent_self_update_unavailable": gettext_lazy(
        "Este agente não se atualiza sozinho (agent_root não configurado): atualize-o pelo comando agent-install."
    ),
    "release_without_agent": gettext_lazy("O pacote de release não traz o agente de deployment."),
    "agent_publisher_not_allowed": gettext_lazy(
        "O publicador deste pacote não pode fornecer o agente: só o publicador do núcleo instalado ou um de agent_publishers."
    ),
    "agent_already_current": gettext_lazy("O agente de deployment já está nesta versão."),
    "agent_downgrade_forbidden": gettext_lazy("O pacote traz um agente mais antigo que o instalado."),
    "agent_target_unavailable": gettext_lazy("O pacote não traz dependências para a plataforma do agente."),
    "agent_build_failed": gettext_lazy(
        "A montagem do agente novo falhou; o agente atual continua em uso. Veja o diário do agente no servidor."
    ),
    # Sources registered on the page (#499).
    "invalid_source": gettext_lazy("Fonte inválida: confira o identificador, o repositório e o canal."),
    "source_id_reserved": gettext_lazy(
        "Este identificador é de uma fonte do arquivo do agente e não pode ser usado aqui."
    ),
    "publisher_not_approved": gettext_lazy(
        "O publicador escolhido não está aprovado no servidor (approved_publishers do agente)."
    ),
    "too_many_sources": gettext_lazy(
        "O limite de fontes cadastradas foi atingido. Remova uma antes de cadastrar outra."
    ),
    "source_not_found": gettext_lazy("A fonte não está cadastrada."),
    "invalid_managed_sources": gettext_lazy(
        "As fontes cadastradas estão inválidas no agente (por exemplo, um publicador deixou de ser aprovado). "
        "Corrija o arquivo sources.json do agente no servidor."
    ),
    "github_token_missing": gettext_lazy("Repositório privado exige o token configurado no arquivo do agente."),
    # Update path and sources (#500).
    "version_jump_forbidden": gettext_lazy(
        "Esta versão está mais de uma linha à frente da instalada. Atualize antes para a versão indicada em Novas versões."
    ),
    "release_environment_conflict": gettext_lazy(
        "Este núcleo já foi recebido com outro conjunto de dependências. O publicador precisa publicar outra versão."
    ),
    "dependency_version_conflict": gettext_lazy(
        "Uma dependência do projeto (project_requirements_lock) fixa outra versão de uma dependência do núcleo."
    ),
    "github_rate_limited": gettext_lazy(
        "O GitHub limitou as consultas deste servidor. Tente de novo mais tarde ou configure um token no agente."
    ),
    "github_release_not_found": gettext_lazy("O repositório não tem release publicado (ou não está acessível)."),
    "source_index_missing": gettext_lazy(
        "O release mais recente ainda não tem o índice assinado. Logo depois de uma publicação, aguarde alguns minutos."
    ),
    "github_asset_missing": gettext_lazy("O arquivo indicado pelo índice não está no release do GitHub."),
    "github_asset_url_rejected": gettext_lazy(
        "O download do GitHub apontou para um endereço não permitido pelo agente."
    ),
    "catalogue_publisher_mismatch": gettext_lazy(
        "O índice desta fonte foi assinado por outro publicador que não o escolhido para ela."
    ),
    "catalogue_artifact_path_rejected": gettext_lazy("O índice desta fonte aponta para um arquivo fora do permitido."),
    "catalogue_unavailable": gettext_lazy(
        "A fonte não respondeu ou respondeu em formato inesperado. Tente de novo mais tarde."
    ),
    "catalogue_download_too_large": gettext_lazy("O arquivo da fonte passa do limite de tamanho do agente."),
    "catalogue_redirect_rejected": gettext_lazy("A fonte tentou redirecionar o download, o que o agente não segue."),
    "package_verification_failed": gettext_lazy(
        "O pacote não passou na verificação: assinatura de publicador não aprovado, conteúdo alterado ou formato inválido."
    ),
    "stored_artifact_corrupted": gettext_lazy(
        "O pacote guardado no agente foi alterado. Avise o operador do servidor."
    ),
    # A cluster of agents (#678-#684).
    "no_primary": gettext_lazy(
        "Nenhum agente primário ativo: atualizações e instalação de apps ficam suspensas até um candidato assumir. "
        "O site continua no ar."
    ),
    "primary_unreachable": gettext_lazy(
        "O agente primário não respondeu pelo canal entre agentes. Confira a rede privada entre os nós."
    ),
    "not_primary": gettext_lazy("Este agente deixou de ser o primário; tente de novo em alguns segundos."),
    "cluster_busy": gettext_lazy("Há uma operação em curso no cluster; espere ela terminar."),
    "recovery_or_operation_pending": gettext_lazy(
        "Há uma operação pendente ou interrompida (por exemplo, depois da troca do agente primário). "
        "O operador precisa concluir a recuperação no servidor antes de outra atualização."
    ),
    "rolling_not_eligible": gettext_lazy("Este plano não pode trocar nó a nó: ele precisa da janela de manutenção."),
    "follower_prepare_failed": gettext_lazy("Um nó não conseguiu receber o release; nada mudou."),
    "follower_drain_failed": gettext_lazy("Um nó não parou a tempo; a atualização foi desfeita antes das migrations."),
    "follower_cutover_failed": gettext_lazy(
        "Um nó não trocou o release depois das migrations. A manutenção continua: veja a recuperação no servidor."
    ),
    "rolling_node_failed": gettext_lazy("Um nó falhou na troca nó a nó e voltou ao release anterior."),
}


def _error(request, exc):
    if isinstance(exc, DeploymentUnavailable):
        messages.error(
            request, gettext("O agente de deployment não está disponível ou não foi configurado nesta instalação.")
        )
        return
    code = getattr(exc, "code", type(exc).__name__)
    if code in ERROR_TEXT:
        messages.error(request, ERROR_TEXT[code])
        return
    messages.error(request, gettext("Operação indisponível: %(value1)s.") % {"value1": code})


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
    "agent_update_required": gettext_lazy(
        "{component} {found} não instala o núcleo {required}: atualize o agente antes do núcleo."
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


def _plan_page(
    request, form, *, status=200, artifact=None, problems=(), plan_action="", plan_artifacts="", agent_self_update=False
):
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
            agent_self_update=agent_self_update,
        ),
        status=status,
    )


def _agent_behind(artifact):
    """The agent inventory when the agent's line is older than the release's core line (#500)."""
    from skeleton.versioning import release_line

    if artifact.get("kind") != "release":
        return None
    try:
        agent = agent_client().inventory().get("agent") or {}
        if agent.get("version") and release_line(agent["version"]) < release_line(
            artifact["core"]["manifest"]["version"]
        ):
            return agent
    except (ValueError, RuntimeError, OSError, KeyError, TypeError):
        return None  # The preview's own agent_update_required check still applies.
    return None


def _agent_self_update(artifact):
    """Whether the agent can replace itself, asked only when a release package offers a newer one."""
    if not (artifact.get("agent") or {}).get("update_required"):
        return False
    try:
        return bool((agent_client().inventory().get("agent") or {}).get("self_update"))
    except (ValueError, RuntimeError, OSError):
        return False


# --------------------------------------------------------------------------- steps


CORE_ORIGIN_LABELS = {
    "installed": gettext_lazy("pacote instalado"),
    "source": gettext_lazy("código-fonte"),
    "unknown": gettext_lazy("desconhecida"),
}


def running_core():
    """The running core release, with a translated label for its origin code (#364)."""
    from skeleton.versioning import core_version

    core = core_version()
    return {**core, "origin_label": CORE_ORIGIN_LABELS.get(core["origin"], core["origin"])}


@platform_page(capability="updates.operate", writes=True)
@require_safe
def installed(request):
    inventory = None
    cluster = {"mode": "single"}
    try:
        inventory = agent_client().inventory()
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    else:
        try:
            cluster = agent_client().cluster()
        except (ValueError, RuntimeError, OSError):
            cluster = {"mode": "single"}  # An agent of one node (or older): no cluster panel.
    return render(
        request,
        "skeleton/web/platform/updates/installed.html",
        _context(
            "installed",
            core=running_core(),
            inventory=inventory,
            cluster=cluster,
            components=ComponentInstallation.objects.order_by("kind", "component_id"),
        ),
    )


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["POST"])
@recent_authentication_required
def cluster_restart(request):
    """Restart the nodes one at a time with the same release (#682), through this node's agent."""
    try:
        agent_client().cluster_restart()
        messages.success(request, gettext("Reinício em rolagem iniciado: os nós reiniciam um de cada vez."))
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    return redirect("platform:updates")


BLOCKED_TEXT = {
    "missing_intermediate_release": gettext_lazy(
        "Falta publicar uma versão intermediária: esta instalação não pode pular direto para {latest}."
    ),
    "incompatible_runtime": gettext_lazy(
        "As versões mais novas não são compatíveis com o Python ou o núcleo desta instalação."
    ),
}


def _text(value, maximum=200):
    return value if isinstance(value, str) and 0 < len(value) <= maximum else None


CORE_COMPONENT = "django-skeleton"
# The core version the operator chose (#653): only a preference of this session, never trust; the
# agent revalidates every step, and the target is checked against the fresh paths on every use.
TARGET_SESSION_KEY = "skeleton_updates_core_target"
TARGET_UNREACHABLE = gettext_lazy(
    "A versão {target} escolhida deixou de ser alcançável a partir da instalada (índice vencido, "
    "versão retirada ou linha intermediária faltando). Verifique as fontes e escolha outra."
)


def _steps(value):
    return [
        {"version": step["version"], "source_id": step["source_id"]}
        for step in (value if isinstance(value, list) else [])
        if isinstance(step, dict) and _text(step.get("version")) and _text(step.get("source_id"))
    ]


def update_paths(status, names=None):
    """Cards of the agent's update paths (#500); unexpected shapes are dropped, never a 500."""
    names = names or {}
    cards = []
    for path in status.get("paths") or () if isinstance(status, dict) else ():
        if not isinstance(path, dict) or not _text(path.get("component_id")):
            continue
        steps = _steps(path.get("steps"))
        targets = []
        if path["component_id"] == CORE_COMPONENT:
            for target in path.get("targets") if isinstance(path.get("targets"), list) else ():
                target_steps = _steps(target.get("steps")) if isinstance(target, dict) else []
                if target_steps and _text(target.get("version")) == target_steps[-1]["version"]:
                    targets.append({"version": target["version"], "steps": target_steps})
        latest = _text(path.get("latest"))
        blocked = BLOCKED_TEXT.get(path.get("blocked"))
        component = path["component_id"]
        default = gettext("Núcleo") if component == CORE_COMPONENT else component
        cards.append(
            {
                "component_id": component,
                "name": names.get(component) or default,
                "installed": _text(path.get("installed")) or "—",
                "latest": latest,
                "steps": steps,
                "blocked": str(blocked).format(latest=latest or "—") if blocked else None,
                "targets": targets,
                "target": None,
            }
        )
    return cards


def _version_at_least(installed, target):
    try:
        return Version(installed) >= Version(target)
    except (InvalidVersion, TypeError):
        return False


def toward_target(cards, target):
    """The core card steered to the chosen ``target`` (#653); returns ``(cards, reached)``.

    Reachable: the steps to it. Reached (installed at or above it): no core step. Otherwise no
    step and the refusal, so a stale choice never falls back to another version silently.
    """
    if not target:
        return cards, False
    reached = False
    for card in cards:
        if card["component_id"] != CORE_COMPONENT:
            continue
        chosen = next((item for item in card["targets"] if item["version"] == target), None)
        card["target"] = target
        if _version_at_least(card["installed"], target):
            card["steps"], card["blocked"], reached = [], None, True
        elif chosen is not None:
            card["steps"], card["blocked"] = chosen["steps"], None
        else:
            card["steps"], card["blocked"] = [], str(TARGET_UNREACHABLE).format(target=target)
    return cards, reached


def _source_rows(status):
    rows = []
    for source in status.get("sources") or () if isinstance(status, dict) else ():
        if isinstance(source, dict) and _text(source.get("id")):
            code = source.get("error_code")
            rows.append(
                {
                    "id": source["id"],
                    "state": _text(source.get("state")) or "—",
                    "error_code": _text(code),
                    "error": ERROR_TEXT.get(code) if code else None,
                }
            )
    return rows


def _component_names():
    """Readable names of installed apps, from the agent inventory; only a nicety."""
    try:
        inventory = agent_client().inventory()
    except (ValueError, RuntimeError, OSError):
        return {}
    return {
        entry["manifest"]["app_id"]: entry["manifest"].get("name") or entry["manifest"]["app_id"]
        for entry in inventory.get("apps", [])
        if isinstance(entry, dict) and isinstance(entry.get("manifest"), dict) and entry["manifest"].get("app_id")
    }


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["GET", "POST"])
def available(request):
    """The last check and the update path, one step at a time (#500); POST checks again."""
    if request.method == "POST":
        try:
            agent_client().check_updates()
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
        invalidate_update_notice()
        return redirect("platform:updates-available")
    status = None
    try:
        status = agent_client().update_status()
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    status = status if isinstance(status, dict) else {}
    paths = update_paths(status, _component_names() if status.get("paths") else {})
    target = _text(request.GET.get("target"), 64)
    if target is None:
        target = request.session.get(TARGET_SESSION_KEY)
    elif target == "latest":  # The default path again: forget the chosen version.
        request.session.pop(TARGET_SESSION_KEY, None)
        target = None
    paths, reached = toward_target(paths, target)
    if reached:
        request.session.pop(TARGET_SESSION_KEY, None)
    return render(
        request,
        "skeleton/web/platform/updates/available.html",
        _context(
            "available",
            updates=status,
            loaded=bool(status),
            checked_at=_text(status.get("checked_at")),
            sources=_source_rows(status),
            paths=[card for card in paths if card["latest"]],
            current=[card for card in paths if not card["latest"]],
            releases=[row for row in status.get("releases") or () if isinstance(row, dict)],
        ),
    )


@csrf_exempt
@platform_page(capability="updates.operate", writes=True)
def upload(request):
    # Install the bounded streaming handler before CSRF parses multipart data. It replaces the
    # global BoundedUploadHandler here only: a package may be larger than an attachment (#242).
    from skeleton.common.uploads import BoundedUploadHandler

    request.upload_handlers = [
        BoundedPackageUpload(request),
        *(handler for handler in request.upload_handlers if not isinstance(handler, BoundedUploadHandler)),
    ]
    return _upload(request)


@csrf_protect
@recent_authentication_required(allow_get=True)
def _upload(request):
    require_release_operator(request.user)
    if request.method != "POST":
        return render(
            request, "skeleton/web/platform/updates/upload.html", _context("upload", upload_form=UploadForm())
        )
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
            digest, action = plan_artifact(artifact)
            return _plan_page(
                request,
                PlanForm(initial={"artifacts": digest, "action": action or "install"}),
                artifact=artifact,
                agent_self_update=_agent_self_update(artifact),
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
    form = PlanForm(request.POST)
    if form.is_valid():
        try:
            plan_id = request_preview(user=request.user, **form.cleaned_data)
            return redirect("platform:updates-preview", plan_id=plan_id)
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
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def preview(request, plan_id):
    """Follow a plan the agent prepares (#298). GET only reads; Continue (POST) records the operation."""
    if request.method == "POST":
        try:
            row = record_preview(user=request.user, plan_id=plan_id)
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
            return redirect("platform:updates-preview", plan_id=plan_id)
        return redirect("platform:updates-operation", operation_id=row.pk)
    try:
        status = preview_status(user=request.user, plan_id=plan_id)
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
        return redirect("platform:updates-plan")
    if status["state"] == "failed":
        source = status.get("request") or {}
        form = PlanForm(
            {
                "action": source.get("action", ""),
                "artifacts": " ".join(source.get("artifacts") or ()),
                "app_ids": " ".join(source.get("app_ids") or ()),
            }
        )
        form.is_valid()
        return _preview_failure(request, form, _PreparationFailed(status))
    return render(
        request,
        "skeleton/web/platform/updates/preview.html",
        _context(
            "plan",
            plan_id=str(plan_id),
            plan_state=status["state"],
            plan=status.get("plan") or {},
            theme_warnings=uninstall_theme_warnings(status.get("request") or {}),
        ),
    )


def uninstall_theme_warnings(request_data):
    """The instance themes that leave with an uninstall plan (#644), from the code still loaded here.

    Only informative: nothing is added to what the operator approves, and the plan digest is the
    agent's. ``[{"app", "names", "active"}]``; ``active`` is the name of the active theme, if any.
    """
    if request_data.get("action") != "uninstall":
        return []
    from skeleton.appearance.app_themes import themes_of_app
    from skeleton.system.app_registry import all_apps

    leaving = set(request_data.get("app_ids") or ())
    warnings = []
    for manifest in all_apps():
        if (manifest.app_id or manifest.app_label) not in leaving or not manifest.instance_themes:
            continue
        themes = [theme for theme in themes_of_app(manifest) if theme["valid"]]
        if themes:
            active = next((theme["name"] for theme in themes if theme["active"]), "")
            warnings.append({"app": manifest.label, "names": ", ".join(t["name"] for t in themes), "active": active})
    return warnings


class _PreparationFailed(AgentError):
    """A failed preparation shown exactly like a synchronous preview failure."""

    def __init__(self, status):
        code = status.get("error")
        if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 100:
            code = "agent_request_failed"
        super().__init__(code, 409, sanitize_details(status.get("details")))


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["POST"])
@recent_authentication_required
def add_dependency(request):
    """Fetch an operator-chosen catalogue suggestion and preview the plan again with it."""
    identifiers = {name: request.POST.get(name, "") for name in ("component_id", "version", "source_id")}
    if any(not value or len(value) > 128 for value in identifiers.values()):
        return HttpResponse(gettext("Identificação de release inválida."), status=400)
    form = PlanForm({"action": request.POST.get("action", ""), "artifacts": request.POST.get("artifacts", "")})
    if not form.is_valid() or form.cleaned_data["action"] in {"uninstall", "reinstall"}:
        return HttpResponse(gettext("Plano de origem inválido."), status=400)
    try:
        artifact = agent_client().fetch_release(**identifiers)
        action = "update" if request.POST.get("upgrade") == "1" else form.cleaned_data["action"]
        artifacts = list(dict.fromkeys([*form.cleaned_data["artifacts"], plan_artifact(artifact)[0]]))
        form = PlanForm({"action": action, "artifacts": " ".join(artifacts)})
        form.is_valid()
        plan_id = request_preview(user=request.user, action=action, artifacts=artifacts)
        return redirect("platform:updates-preview", plan_id=plan_id)
    except (ValueError, RuntimeError, OSError) as exc:
        return _preview_failure(request, form, exc)


def _target_refusal(identifiers, target):
    """Why the posted core ``target`` cannot be followed now (#653), or ``None``.

    The form is untrusted: the target must be one the fresh paths offer, and the step must be
    the first one toward it. The agent still revalidates the step (lines, downgrade, signature).
    """
    if len(target) > 64:
        return str(TARGET_UNREACHABLE).format(target="—")
    cards, _reached = toward_target(update_paths(agent_client().update_status()), target)
    core = next((card for card in cards if card["component_id"] == CORE_COMPONENT), None)
    if core is None or not core["steps"]:
        return core["blocked"] if core and core["blocked"] else str(TARGET_UNREACHABLE).format(target=target)
    first = core["steps"][0]
    if (first["version"], first["source_id"]) != (identifiers["version"], identifiers["source_id"]):
        return str(TARGET_UNREACHABLE).format(target=target)
    return None


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["POST"])
@recent_authentication_required
def fetch_release(request):
    try:
        identifiers = {name: request.POST.get(name, "") for name in ("component_id", "version", "source_id")}
        if any(not value or len(value) > 128 for value in identifiers.values()):
            return HttpResponse(gettext("Identificação de release inválida."), status=400)
        target = request.POST.get("target", "")
        if identifiers["component_id"] == CORE_COMPONENT and target:
            refusal = _target_refusal(identifiers, target)
            if refusal is not None:
                messages.error(request, refusal)
                return redirect("platform:updates-available")
            request.session[TARGET_SESSION_KEY] = target
        elif identifiers["component_id"] == CORE_COMPONENT:
            request.session.pop(TARGET_SESSION_KEY, None)
        artifact = agent_client().fetch_release(**identifiers)
        digest, action = plan_artifact(artifact)
        blocking = _agent_behind(artifact)
        if blocking is not None:
            return render(
                request,
                "skeleton/web/platform/updates/agent-first.html",
                _context("available", artifact=artifact, agent=blocking),
            )
        if action is None and identifiers["component_id"] == "django-skeleton":
            action = "core_update"
        elif action is None:
            action = "update" if identifiers["component_id"] in _installed_components() else "install"
        messages.success(request, gettext("Versão obtida e verificada. Confira o plano e visualize o impacto."))
        return _plan_page(
            request,
            PlanForm(initial={"action": action, "artifacts": digest}),
            artifact=artifact,
            agent_self_update=_agent_self_update(artifact),
        )
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
        return redirect("platform:updates-available")


def _sources_page(request, form=None, *, listing=None, status=200):
    listing = listing or {}
    rows = listing.get("sources") if isinstance(listing.get("sources"), list) else []
    publishers = [value for value in listing.get("publishers", []) if isinstance(value, str)]
    private_available = bool(listing.get("private_available"))
    if form is None and publishers:
        initial = {"channel": "stable"}
        if not any(isinstance(row, dict) and row.get("type") == "github" for row in rows):
            initial.update(source_id="skeleton", repository=UPSTREAM_REPOSITORY)
        form = SourceForm(initial=initial, publishers=publishers, private_available=private_available)
    return render(
        request,
        "skeleton/web/platform/updates/sources.html",
        _context(
            "sources",
            sources=[row for row in rows if isinstance(row, dict)],
            source_form=form if publishers else None,
            publishers=publishers,
            listing_loaded=bool(listing),
        ),
        status=status,
    )


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def sources(request):
    """The release sources the agent consults; registering one changes only where it looks (#499)."""
    try:
        listing = update_sources(user=request.user)
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
        return _sources_page(request)
    if request.method != "POST":
        return _sources_page(request, listing=listing)
    form = SourceForm(
        request.POST,
        publishers=[value for value in listing.get("publishers", []) if isinstance(value, str)],
        private_available=bool(listing.get("private_available")),
    )
    if not form.is_valid():
        return _sources_page(request, form, listing=listing, status=400)
    try:
        save_update_source(
            user=request.user,
            source_id=form.cleaned_data["source_id"],
            repository=form.cleaned_data["repository"],
            publisher=form.cleaned_data["publisher"],
            channel=form.cleaned_data["channel"],
            private=form.cleaned_data.get("private", False),
        )
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
        return _sources_page(request, form, listing=listing, status=400)
    messages.success(request, gettext("Fonte salva. Ela passa a ser consultada em Novas versões."))
    return redirect("platform:updates-sources")


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["POST"])
@recent_authentication_required
def remove_source(request):
    source_id = request.POST.get("source_id", "")
    if not re.fullmatch("[a-z][a-z0-9-]{0,31}", source_id):
        return HttpResponse(gettext("Identificador de fonte inválido."), status=400)
    try:
        remove_update_source(user=request.user, source_id=source_id)
        messages.success(request, gettext("Fonte removida."))
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    return redirect("platform:updates-sources")


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["POST"])
@recent_authentication_required
def update_agent(request):
    """Replace the agent with the one of an uploaded release package (#497); the agent restarts."""
    digest = request.POST.get("release", "")
    if not re.fullmatch("[0-9a-f]{64}", digest):
        return HttpResponse(gettext("Digest de pacote inválido."), status=400)
    try:
        request_agent_update(user=request.user, digest=digest)
        messages.success(request, gettext("O agente está reiniciando. Atualize a página em alguns segundos."))
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    return redirect("platform:updates")


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
    # No row lock while the agent answers (#250): confirm and retry lock on their own.
    row = get_object_or_404(DeploymentOperation.objects, pk=operation_id)
    if request.method == "POST":
        form = ConfirmationForm(request.POST)
        if not form.is_valid() or form.cleaned_data["plan_digest"] != row.plan_digest:
            return HttpResponse(gettext("Confirmação do plano inválida."), status=400)
        try:
            if request.POST.get("intent") == "retry":
                retry_submission(user=request.user, operation_id=row.pk, revision=form.cleaned_data["revision"])
            else:
                confirm_operation(
                    user=request.user,
                    operation_id=row.pk,
                    revision=form.cleaned_data["revision"],
                    force_window=request.POST.get("force_window") == "on",
                )
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
            status = agent_client().operation(str(row.pk))  # No row lock while waiting for the agent.
            row = DeploymentOperation.objects.select_for_update().get(pk=row.pk)
            reconcile_operation(row, status)
            status_url = status.get("status_url")
    except (ConcurrentUpdate, ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    next_steps = None
    if row.state == "succeeded":
        try:
            cards, reached = toward_target(
                update_paths(agent_client().update_status()), request.session.get(TARGET_SESSION_KEY)
            )
            if reached:
                request.session.pop(TARGET_SESSION_KEY, None)
            next_steps = [card for card in cards if card["steps"] or (card["target"] and card["blocked"])]
        except (ValueError, RuntimeError, OSError, TypeError):
            next_steps = None  # Only hides the section; the operation page never breaks for it.
    return render(
        request,
        "skeleton/web/platform/updates/operation.html",
        _context(
            "operations",
            next_steps=next_steps,
            operation=row,
            confirmation_form=ConfirmationForm(
                initial={"revision": row.revision, "plan_digest": row.plan_digest},
                rolling=(row.plan or {}).get("rollout_mode") == "rolling",
            ),
            status_url=status_url,
            can_retry=submission_can_retry(row),
        ),
    )
