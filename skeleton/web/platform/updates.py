"""Versions and updates of the platform (/platform/updates/), in steps.

The web process only asks the independent deployment agent for previews and
approvals; it never runs pip, migrations or DDL. Mutations require a platform
administrator, CSRF and a recent authentication. The upload handler is bounded
and installed before CSRF parses the multipart body, in place of the global
attachment bound (``SKELETON_PACKAGE_MAX_BYTES`` instead of ``SKELETON_UPLOAD_MAX_BYTES``).
"""

import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods, require_safe
from packaging.version import InvalidVersion, Version

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system.deployment_client import AgentError, sanitize_details
from skeleton.system.models import ComponentInstallation, DeploymentOperation
from skeleton.system.releases import (
    CLOSED_PREVIEW_STATES,
    GITHUB_REPOSITORY,
    UPSTREAM_REPOSITORY,
    DeploymentUnavailable,
    agent_client,
    confirm_operation,
    discard_preview,
    invalidate_update_notice,
    plan_artifact,
    plan_scope,
    preview_is_stale,
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
from skeleton.web.confirm import PLATFORM_TEMPLATE, Confirmation, confirmation_step

from .guards import platform_page

__all__ = [
    "add_dependency",
    "agent_wait",
    "apps_overview",
    "available",
    "discard",
    "fetch_release",
    "installed",
    "operation",
    "operations",
    "plan",
    "preview",
    "remove_source",
    "sources",
    "status_link",
    "update_agent",
    "upload",
]

# Two areas (#822): where the update of the platform (core and agent) and that of the apps start.
# The engine is one: a core plan may carry apps, so plan, approval and follow-up are shared and
# take the area of the operation.
PLATFORM, APPS = "platform", "apps"
AREAS = {
    PLATFORM: {
        "section": "updates",
        "url_name": "platform:updates",
        "eyebrow": gettext_lazy("Plataforma"),
        "title": gettext_lazy("Atualizações da plataforma"),
        "description": gettext_lazy(
            "Veja a versão do núcleo e do agente de deployment e atualize a plataforma. "
            "Nada é aplicado sem que você confira o impacto e aprove."
        ),
        "tabs": (
            ("overview", gettext_lazy("Visão geral"), "platform:updates", ""),
            ("history", gettext_lazy("Histórico"), "platform:updates-operations", "?area=platform"),
            ("sources", gettext_lazy("Fontes"), "platform:updates-sources", ""),
            ("upload", gettext_lazy("Enviar pacote"), "platform:updates-upload", "?area=platform"),
        ),
    },
    APPS: {
        "section": "updates-apps",
        "url_name": "platform:updates-apps",
        "eyebrow": gettext_lazy("Aplicativos"),
        "title": gettext_lazy("Atualizações de aplicativos"),
        "description": gettext_lazy(
            "Instale, atualize ou retire aplicativos desta instância. "
            "Cada mudança passa por uma prévia do impacto antes de ser aprovada."
        ),
        "tabs": (
            ("overview", gettext_lazy("Visão geral"), "platform:updates-apps", ""),
            ("history", gettext_lazy("Histórico"), "platform:updates-operations", "?area=apps"),
            ("upload", gettext_lazy("Enviar pacote"), "platform:updates-upload", "?area=apps"),
        ),
    },
}
# The steps of one plan (#823): the only numbered sequence of the area.
FLOW_STEPS = (
    ("choose", gettext_lazy("Escolher")),
    ("review", gettext_lazy("Conferir o impacto")),
    ("approve", gettext_lazy("Aprovar")),
    ("follow", gettext_lazy("Acompanhar")),
)
# Operation states that are still running or waiting for the agent.
ACTIVE_STATES = ("approved", "queued", "running", "submission_failed", "interrupted")


def area_of(action):
    """The area of a plan or operation: the core update is the platform's, the rest the apps'."""
    return PLATFORM if action == "core_update" else APPS


def _area(value):
    return value if value in AREAS else PLATFORM


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


def plan_heading(values, artifact):
    """ "Atualizar Notas para 1.4.0": the heading of a prefilled plan (#823); the action otherwise."""
    manifest = artifact.get("manifest") if isinstance(artifact, dict) else None
    manifest = manifest if isinstance(manifest, dict) else {}
    version = _text(manifest.get("version"), 64)
    if isinstance(artifact, dict) and artifact.get("kind") == "release":
        core = artifact.get("core") if isinstance(artifact.get("core"), dict) else {}
        version = _text((core.get("manifest") or {}).get("version"), 64) or version
    name = _text(manifest.get("name"), 200) or _text(manifest.get("app_id"), 200)
    action = values["action"]
    if action == "core_update" and version:
        return gettext("Atualizar o núcleo para %(version)s") % {"version": version}
    if action == "update" and name and version:
        return gettext("Atualizar %(name)s para %(version)s") % {"name": name, "version": version}
    if action == "install" and name and version:
        return gettext("Instalar %(name)s %(version)s") % {"name": name, "version": version}
    if action == "uninstall" and values["app_ids"]:
        return gettext("Retirar o código de %(apps)s") % {"apps": values["app_ids"].replace(" ", ", ")}
    return values["action_label"]


def _plan_values(form):
    """``{action, action_label, artifacts, app_ids}`` of a plan form, bound or prefilled (#823)."""
    if form.is_bound:
        raw = {name: form.data.get(name, "") for name in ("action", "artifacts", "app_ids")}
    else:
        raw = {name: form.initial.get(name, "") for name in ("action", "artifacts", "app_ids")}
    values = {
        name: " ".join(value) if isinstance(value, (list, tuple)) else str(value or "") for name, value in raw.items()
    }
    values["action_label"] = dict(form.fields["action"].choices).get(values["action"], values["action"])
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


def _context(area, tab=None, *, flow=None, **values):
    """The area (menu, header and tabs) and, on the pages of one plan, its step (#822, #823)."""
    meta = AREAS[_area(area)]
    tabs = [
        {"id": tab_id, "label": label, "url": reverse(url_name) + query, "current": tab_id == tab}
        for tab_id, label, url_name, query in meta["tabs"]
    ]
    steps = None
    if flow is not None:
        index = [step_id for step_id, _label in FLOW_STEPS].index(flow)
        steps = [
            {"number": number, "label": label, "current": number - 1 == index, "done": number - 1 < index}
            for number, (_step_id, label) in enumerate(FLOW_STEPS, start=1)
        ]
    return {
        "platform_section": meta["section"],
        "area": _area(area),
        "area_meta": {**meta, "url": reverse(meta["url_name"]), "tabs": tabs},
        "flow_steps": steps,
        "updates_help": guide_url(),
        **values,
    }


# Operator-facing text of the agent update codes (#497); any other code is shown as is.
ERROR_TEXT = {
    # The agent did not answer on its control socket (#776): say what it means, not the code.
    "agent_unavailable": gettext_lazy(
        "O agente de deployment não respondeu. Confira se o serviço do agente está no ar e tente de novo."
    ),
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
        "Esta versão está mais de uma linha à frente da instalada. Siga os passos indicados em Atualizações da plataforma."
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
    "agent_versions_differ": gettext_lazy(
        "Os agentes dos nós estão em versões diferentes. Atualize o agente antes; nos containers, a imagem de cada nó."
    ),
    # Problems a node reports in its status (#678, #689).
    "agent_update_manual": gettext_lazy(
        "Este nó roda o agente da imagem do container e não segue a versão do cluster sozinho: "
        "atualize a imagem dele e rode docker compose up -d."
    ),
    "cluster_storage_not_shared": gettext_lazy(
        "Os candidatos não dividem o mesmo storage_root: nenhum deles assume como primário até a montagem "
        "compartilhada ser corrigida."
    ),
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
    messages.error(
        request,
        gettext("Não foi possível concluir a operação. Código para o suporte: %(value1)s.") % {"value1": code},
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
    request, form, *, status=200, artifact=None, problems=(), plan_action="", plan_artifacts="", agent_offer=None
):
    """The plan before its preview (#823): a summary and one button when the plan came prefilled.

    The full form (operation, digests, app IDs) is the advanced, manual path; it opens by itself
    when it comes back with an error.
    """
    values = _plan_values(form)
    if form.is_bound:
        manual = bool(form.errors)
    else:
        manual = not (values["artifacts"] or values["app_ids"])
    return render(
        request,
        "skeleton/web/platform/updates/plan.html",
        _context(
            area_of(plan_action or values["action"]),
            flow="choose",
            plan_manual=manual,
            plan_values=values,
            plan_heading=plan_heading(values, artifact),
            plan_form=form,
            artifact=artifact,
            problems=problems,
            plan_action=plan_action,
            plan_artifacts=plan_artifacts,
            agent_offer=agent_offer,
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


# A path the screen prints inside a shell command (#775): absolute and without anything a shell
# would interpret. Anything else falls back to the generic text, never to a half-quoted command.
SHELL_PATH = re.compile(r"/[A-Za-z0-9._/@+-]{0,400}")


def manual_update(agent, digest=None):
    """The closed shape of the agent's ``manual_update`` (#775), or ``None`` for the generic text.

    ``container``: the agent comes from the image. ``systemd``: the real paths of the one-time
    migration to ``agent_root``, and the uploaded package's path when its ``digest`` is known.
    """
    raw = agent.get("manual_update") if isinstance(agent, dict) else None
    if not isinstance(raw, dict):
        return None
    if raw.get("layout") == "container":
        return {"layout": "container"}
    if raw.get("layout") != "systemd":
        return None
    paths = {key: raw.get(key) for key in ("executable", "config_path", "suggested_root", "uploads_dir")}
    if not all(isinstance(value, str) and SHELL_PATH.fullmatch(value) for value in paths.values()):
        return None
    result = {"layout": "systemd", **paths}
    if isinstance(digest, str) and DIGEST.fullmatch(digest):
        # The same name Store.path("uploads", digest) gives the package in deployment_agent/storage.py.
        result["package"] = f"{paths['uploads_dir'].rstrip('/')}/{digest}.skapp"
    return result


def _agent_offer(artifact):
    """How the agent of a release package that offers a newer one gets installed here, or ``None``.

    ``{"self_update": bool, "manual": manual_update(...)}``; the inventory is asked only then.
    """
    if not (artifact.get("agent") or {}).get("update_required"):
        return None
    try:
        agent = agent_client().inventory().get("agent") or {}
    except (ValueError, RuntimeError, OSError):
        return {"self_update": False, "manual": None}
    return {"self_update": bool(agent.get("self_update")), "manual": manual_update(agent, artifact.get("digest"))}


# The agent update that a core step starts (#773): after the restart, the wait page goes on to the
# plan by itself. Only identifiers kept in this session, never trust: the new agent fetches and
# checks the package again, and the operator still confirms the plan.
AGENT_CONTINUATION_KEY = "platform.updates.agent_continuation"
AGENT_WAIT_LIMIT = timedelta(minutes=30)
DIGEST = re.compile("[0-9a-f]{64}")


def _identifiers(values):
    """``component_id``, ``version`` and ``source_id`` of a catalogue step, or ``None``."""
    identifiers = {name: values.get(name, "") for name in ("component_id", "version", "source_id")}
    if all(isinstance(value, str) and 0 < len(value) <= 128 for value in identifiers.values()):
        return identifiers
    return None


def _start_agent_update(request, *, digest, required, action, identifiers=None, artifacts=None):
    """Ask the agent to replace itself and remember where to go after it restarts (#773).

    Raises what the agent raises, before anything is kept in the session. Without a version to wait
    for, only the old notice: the operator comes back to the step by hand.
    """
    result = request_agent_update(user=request.user, digest=digest)
    version = _text(result.get("version") if isinstance(result, dict) else None, 64) or _text(required, 64)
    try:
        Version(version or "")
    except InvalidVersion:
        messages.success(request, gettext("O agente está reiniciando. Atualize a página em alguns segundos."))
        return redirect("platform:updates")
    request.session[AGENT_CONTINUATION_KEY] = {
        "digest": digest,
        "required": version,
        "action": action,
        "identifiers": identifiers,
        "artifacts": artifacts if isinstance(artifacts, str) and DIGEST.fullmatch(artifacts) else None,
        "started_at": timezone.now().isoformat(),
    }
    return redirect("platform:updates-agent-wait")


def _continuation(request):
    """The continuation kept in the session, checked again; a malformed one is dropped."""
    raw = request.session.get(AGENT_CONTINUATION_KEY)
    try:
        started = datetime.fromisoformat(raw["started_at"])
        Version(raw["required"])
        valid = bool(
            DIGEST.fullmatch(raw["digest"])
            and raw["action"] == "core_update"
            and (raw["identifiers"] is None or _identifiers(raw["identifiers"]) == raw["identifiers"])
            and (raw["artifacts"] is None or DIGEST.fullmatch(raw["artifacts"]))
            and started.tzinfo is not None
        )
    except (KeyError, TypeError, ValueError, AttributeError, InvalidVersion):
        valid = False
    if not valid:
        request.session.pop(AGENT_CONTINUATION_KEY, None)
        return None
    return {**raw, "started_at": started}


def _agent_first(request, artifact, agent, *, identifiers=None, artifacts=None, automatic=True):
    """A core step that needs a newer agent (#500, #773).

    With an agent that replaces itself, the step starts by updating it and goes on to the wait page;
    otherwise (or when the agent refuses), "Atualize o agente primeiro" says what to do here.
    """
    offer = artifact.get("agent") or {}
    if automatic and offer.get("update_required") and agent.get("self_update"):
        try:
            return _start_agent_update(
                request,
                digest=artifact["digest"],
                required=offer.get("version"),
                action="core_update",
                identifiers=identifiers,
                artifacts=artifacts,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
    return render(
        request,
        "skeleton/web/platform/updates/agent-first.html",
        _context(
            PLATFORM,
            flow="choose",
            artifact=artifact,
            agent=agent,
            identifiers=identifiers,
            plan_artifacts=artifacts,
            manual=manual_update(agent, artifact.get("digest")),
        ),
    )


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


# The guide section for an agent updated by hand, by layout (#775).
AGENT_GUIDE_ANCHORS = {"container": "atualizar-o-agente-nos-containers-772", "systemd": "migrar-para-agent_root-775"}


def agent_guide(agent):
    """The documentation of this core release that says how this agent is updated by hand."""
    manual = manual_update(agent)
    anchor = AGENT_GUIDE_ANCHORS.get((manual or {}).get("layout"), "atualizar-o-agente")
    return f"{_documentation_base()}/deployment.md#{anchor}"


def _documentation_base():
    version = running_core().get("version")
    reference = f"v{version}" if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version) else "main"
    return f"https://github.com/{UPSTREAM_REPOSITORY}/blob/{reference}/docs"


def guide_url():
    """The operator's update guide of the running core release (#823), the "Como funciona" link."""
    return f"{_documentation_base()}/updates.md"


def _cluster_panel():
    """The cluster panel of the platform page; an agent of one node (or older) has none."""
    try:
        cluster = agent_client().cluster()
    except (ValueError, RuntimeError, OSError):
        return {"mode": "single"}
    if cluster.get("problem"):
        # A sentence, never the bare code; the code is closed and bounded by sanitize_cluster.
        problem = cluster["problem"]
        cluster = {
            **cluster,
            "problem": ERROR_TEXT.get(problem)
            or gettext("O agente deste nó informou um problema (%(code)s); veja o log dele no servidor.")
            % {"code": problem},
        }
    return cluster


def _update_status(request, *, report=True):
    """The last check of the agent (offline), or ``{}``; ``report`` shows why it is missing."""
    try:
        status = agent_client().update_status()
    except (ValueError, RuntimeError, OSError) as exc:
        if report:
            _error(request, exc)
        return {}
    return status if isinstance(status, dict) else {}


def _check_summary(status):
    """When the sources were last consulted and how each one answered."""
    return {"loaded": bool(status), "checked_at": _text(status.get("checked_at")), "sources": _source_rows(status)}


def describe_plan(plan):
    """What a plan installs or takes out, for a list (#821): "Núcleo 0.9.0", "org.example.notes 1.4.0"."""
    if not isinstance(plan, dict):
        return ""
    components = plan.get("components") if isinstance(plan.get("components"), list) else []
    versions = {
        component["component_id"]: component.get("version")
        for component in components
        if isinstance(component, dict) and isinstance(component.get("component_id"), str)
    }
    parts = []
    for component in sorted(plan_scope(plan), key=lambda item: (item != CORE_COMPONENT, item)):
        name = gettext("Núcleo") if component == CORE_COMPONENT else component
        version = None if plan.get("action") == "uninstall" else _text(versions.get(component), 64)
        parts.append(f"{name} {version}" if version else name)
    return ", ".join(parts)


def _described(rows):
    rows = list(rows)
    for row in rows:
        row.target = describe_plan(row.plan)
    return rows


def open_previews(area):
    """Plans of the area awaiting approval (#821, #822), the newest first."""
    rows = DeploymentOperation.objects.filter(state="preview").order_by("-created_at")
    rows = rows.filter(operation="core_update") if area == PLATFORM else rows.exclude(operation="core_update")
    return _described(rows[:10])


def active_operations():
    """Operations approved and not finished yet, of any area: only one runs at a time."""
    return list(DeploymentOperation.objects.filter(state__in=ACTIVE_STATES).order_by("-created_at")[:5])


def _chosen_target(request):
    """The core version the operator chose (#653): ``?target=`` or the one kept in the session."""
    target = _text(request.GET.get("target"), 64)
    if target is None:
        return request.session.get(TARGET_SESSION_KEY)
    if target == "latest":  # The default path again: forget the chosen version.
        request.session.pop(TARGET_SESSION_KEY, None)
        return None
    return target


@platform_page(capability="updates.operate", writes=True)
@require_safe
def installed(request):
    """Atualizações da plataforma (#822): the core and the agent, their next step and the open plans."""
    inventory, cluster, status = None, {"mode": "single"}, {}
    try:
        inventory = agent_client().inventory()
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    else:
        cluster = _cluster_panel()
        status = _update_status(request)
    inventory_data = inventory if isinstance(inventory, dict) else {}
    paths = update_paths(status, _component_names(inventory_data))
    paths, reached = toward_target(paths, _chosen_target(request))
    if reached:
        request.session.pop(TARGET_SESSION_KEY, None)
    mark_agent_first(paths, inventory_data.get("agent"))
    core_card = next((card for card in paths if card["component_id"] == CORE_COMPONENT), None)
    return render(
        request,
        "skeleton/web/platform/updates/platform.html",
        _context(
            PLATFORM,
            "overview",
            core=running_core(),
            inventory=inventory,
            agent_guide=agent_guide(inventory_data.get("agent")),
            cluster=cluster,
            check=_check_summary(status),
            core_card=core_card,
            app_updates=sum(1 for card in paths if card["component_id"] != CORE_COMPONENT and card["latest"]),
            pending=open_previews(PLATFORM),
            active=active_operations(),
            components=ComponentInstallation.objects.order_by("kind", "component_id"),
            releases=[
                row
                for row in status.get("releases") or ()
                if isinstance(row, dict) and row.get("component_id") == CORE_COMPONENT
            ],
        ),
    )


def app_rows(inventory, cards):
    """One row per app the agent manages (#822), with its update card when it has one."""
    rows = []
    for entry in inventory.get("apps") or () if isinstance(inventory, dict) else ():
        manifest = entry.get("manifest") if isinstance(entry, dict) else None
        if not isinstance(manifest, dict) or not _text(manifest.get("app_id"), 100):
            continue
        app_id = manifest["app_id"]
        card = cards.get(app_id)
        digest = entry.get("digest")
        rows.append(
            {
                "app_id": app_id,
                "name": _text(manifest.get("name"), 200) or app_id,
                "version": _text(manifest.get("version"), 64) or "—",
                "state": _text(entry.get("state"), 32) or "—",
                "digest": digest if isinstance(digest, str) and DIGEST.fullmatch(digest) else None,
                # A project-local app is code of the project, not a package the agent can take out.
                "managed": entry.get("origin") != "project-local",
                "update": card if card and (card["latest"] or card["blocked"]) else None,
            }
        )
    return sorted(rows, key=lambda row: row["name"].lower())


def install_candidates(status, inventory):
    """Apps the sources publish and this instance does not have (#822): the newest of each one."""
    known = {
        entry["manifest"]["app_id"]
        for entry in (inventory.get("apps") or () if isinstance(inventory, dict) else ())
        if isinstance(entry, dict) and isinstance(entry.get("manifest"), dict) and entry["manifest"].get("app_id")
    }
    best = {}
    for row in status.get("releases") or () if isinstance(status, dict) else ():
        if not isinstance(row, dict) or row.get("state") != "available":
            continue
        component, version, source = (_text(row.get(name), 128) for name in ("component_id", "version", "source_id"))
        if not (component and version and source) or component == CORE_COMPONENT or component in known:
            continue
        try:
            parsed = Version(version)
        except InvalidVersion:
            continue
        if component not in best or parsed > best[component][0]:
            entry = {"component_id": component, "version": version, "source_id": source}
            best[component] = (parsed, {**entry, "notes": _text(row.get("notes"), 500) or ""})
    return [best[component][1] for component in sorted(best)]


@platform_page(capability="updates.operate", writes=True)
@require_safe
def apps_overview(request):
    """Atualizações de aplicativos (#822): update, install, reinstall or take out the code of an app."""
    inventory, status = {}, {}
    try:
        inventory = agent_client().inventory()
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    else:
        status = _update_status(request)
    inventory = inventory if isinstance(inventory, dict) else {}
    cards = {
        card["component_id"]: card
        for card in update_paths(status, _component_names(inventory))
        if card["component_id"] != CORE_COMPONENT
    }
    rows = app_rows(inventory, cards)
    return render(
        request,
        "skeleton/web/platform/updates/apps-area.html",
        _context(
            APPS,
            "overview",
            loaded=bool(inventory),
            check=_check_summary(status),
            updates=[row for row in rows if row["update"]],
            apps=rows,
            candidates=install_candidates(status, inventory),
            pending=open_previews(APPS),
            active=active_operations(),
        ),
    )


def _describe_cluster_restart(request):
    return Confirmation(
        title=gettext("Reiniciar os nós em rolagem"),
        message=gettext(
            "Os nós reiniciam um de cada vez com o mesmo release. A instância continua no ar, mas cada nó deixa "
            "de atender por alguns segundos e as requisições dele em andamento se perdem."
        ),
        submit_label=gettext("Reiniciar os nós"),
        cancel_url=reverse("platform:updates"),
        eyebrow=gettext("Atualizações da plataforma"),
        template=PLATFORM_TEMPLATE,
        context={"platform_section": "updates"},
    )


@platform_page(capability="updates.operate", writes=True)
@confirmation_step(_describe_cluster_restart)
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


def _inventory():
    """The agent inventory, or ``{}``, for what is only a nicety on a page."""
    try:
        inventory = agent_client().inventory()
    except (ValueError, RuntimeError, OSError):
        return {}
    return inventory if isinstance(inventory, dict) else {}


def _component_names(inventory):
    """Readable names of installed apps, from the agent inventory; only a nicety."""
    return {
        entry["manifest"]["app_id"]: entry["manifest"].get("name") or entry["manifest"]["app_id"]
        for entry in inventory.get("apps") or ()
        if isinstance(entry, dict) and isinstance(entry.get("manifest"), dict) and entry["manifest"].get("app_id")
    }


def mark_agent_first(cards, agent):
    """Mark the core card whose next step needs a newer agent line (#773), as ``_agent_behind`` decides."""
    from skeleton.versioning import release_line

    installed = _text(agent.get("version"), 64) if isinstance(agent, dict) else None
    for card in cards:
        if card["component_id"] != CORE_COMPONENT or not card["steps"] or installed is None:
            continue
        step = card["steps"][0]["version"]
        try:
            behind = release_line(step) > release_line(installed)
        except (InvalidVersion, TypeError, ValueError):
            behind = False
        if behind:
            card["agent_first"] = {"from": installed, "to": step, "self_update": bool(agent.get("self_update"))}
    return cards


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["GET", "POST"])
def available(request):
    """POST consults the sources again (#500) and goes back to the area that asked (#822).

    The old "Novas versões" page is now the overview of each area: GET goes there, with the
    chosen core version when the link carries one.
    """
    if request.method == "POST":
        try:
            agent_client().check_updates()
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
        invalidate_update_notice()
        return redirect(AREAS[_area(request.POST.get("area"))]["url_name"])
    target = _text(request.GET.get("target"), 64)
    url = reverse("platform:updates")
    return redirect(f"{url}?{urlencode({'target': target})}" if target else url)


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


def upload_action(artifact):
    """The digest to plan and the action an uploaded package asks for (#823), as a fetched step does.

    A release or core package updates the core; an app package updates the installed app or
    installs a new one. The operator still sees the impact before approving.
    """
    digest, action = plan_artifact(artifact)
    if action is not None:
        return digest, action
    manifest = artifact.get("manifest") if isinstance(artifact.get("manifest"), dict) else {}
    if artifact.get("kind") == "core" or manifest.get("app_id") == CORE_COMPONENT:
        return digest, "core_update"
    if not manifest.get("app_id"):
        return digest, "install"
    try:
        installed = _installed_components()
    except (ValueError, RuntimeError, OSError, KeyError, TypeError):
        installed = set()
    return digest, "update" if manifest.get("app_id") in installed else "install"


def _upload_page(request, form, *, status=200):
    area = _area(request.POST.get("area") or request.GET.get("area"))
    return render(
        request,
        "skeleton/web/platform/updates/upload.html",
        _context(area, "upload", upload_form=form),
        status=status,
    )


@csrf_protect
@recent_authentication_required(allow_get=True)
def _upload(request):
    require_release_operator(request.user)
    if request.method != "POST":
        return _upload_page(request, UploadForm())
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
            digest, action = upload_action(artifact)
            return _plan_page(
                request,
                PlanForm(initial={"artifacts": digest, "action": action}),
                artifact=artifact,
                agent_offer=_agent_offer(artifact),
            )
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
    return _upload_page(request, form, status=400)


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def plan(request):
    """The manual plan on GET; POST asks the agent for the preview of a plan (#298)."""
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


# The recovery the agent promises for a plan, in a sentence (#823); any other value is shown as is.
RECOVERY_TEXT = {
    "restore_database_and_previous_release_before_new_writes; operator_repair_to_complete": gettext_lazy(
        "Se algo falhar antes de o tráfego voltar, o agente restaura o banco e a versão anterior sozinho. "
        "Depois que o tráfego volta, a recuperação é feita pelo operador no servidor (veja o guia)."
    ),
}


def plan_changes(plan, inventory):
    """What a plan changes against what is installed (#823): ``[{name, before, after, kind}]``.

    ``plan["components"]`` is the whole target inventory; only the differences are listed. An
    app whose code leaves (data preserved) counts as removed. ``None`` without an inventory.
    """
    if not isinstance(plan, dict) or not isinstance(inventory, dict):
        return None
    try:
        before = {CORE_COMPONENT: inventory["core"]["version"]}
        before.update(
            {
                entry["manifest"]["app_id"]: entry["manifest"]["version"]
                for entry in inventory.get("apps") or ()
                if entry.get("state") != "schema_retained"
            }
        )
    except (KeyError, TypeError, AttributeError):
        return None
    after = {}
    for component in plan.get("components") or ():
        if not isinstance(component, dict) or not _text(component.get("component_id"), 128):
            continue
        if component.get("state", "installed") != "schema_retained":
            after[component["component_id"]] = _text(component.get("version"), 64) or "—"
    names = _component_names(inventory)
    changes = []
    for component in sorted(set(before) | set(after), key=lambda item: (item != CORE_COMPONENT, item)):
        old, new = before.get(component), after.get(component)
        if old == new:
            continue
        changes.append(
            {
                "name": gettext("Núcleo") if component == CORE_COMPONENT else names.get(component, component),
                "before": old,
                "after": new,
                "kind": "install" if old is None else "remove" if new is None else "update",
            }
        )
    return changes


def plan_summary(plan, inventory):
    """The plain-language summary that opens the preview and the approval (#823)."""
    migrations = [item for item in (plan.get("migrations") or ()) if isinstance(item, dict)]
    irreversible = any(
        isinstance(step, dict) and step.get("reversible") is False
        for item in migrations
        for step in item.get("operations") or ()
    )
    recovery = plan.get("recovery")
    return {
        "changes": plan_changes(plan, inventory),
        "rolling": plan.get("rollout_mode") == "rolling",
        "rollout_reason": plan.get("rollout_reason"),
        "backup": plan.get("backup_required", True) is not False,
        "migrations": len(migrations),
        "irreversible": irreversible,
        "recovery": RECOVERY_TEXT.get(recovery, recovery) if isinstance(recovery, str) else None,
        "dropped": [name for name in plan.get("dropped_dependencies") or () if isinstance(name, str)],
        "counts": {
            "files": len(plan.get("files") or ()),
            "resources": len(plan.get("resources") or ()),
            "presentation": len(plan.get("presentation_changes") or ()),
        },
    }


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
        if getattr(row, "replaced", 0):
            messages.info(request, gettext("O plano anterior para os mesmos componentes foi substituído por este."))
        return redirect("platform:updates-operation", operation_id=row.pk)
    try:
        status = preview_status(user=request.user, plan_id=plan_id)
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
        return redirect("platform:updates")
    source = status.get("request") or {}
    if status["state"] == "failed":
        form = PlanForm(
            {
                "action": source.get("action", ""),
                "artifacts": " ".join(source.get("artifacts") or ()),
                "app_ids": " ".join(source.get("app_ids") or ()),
            }
        )
        form.is_valid()
        return _preview_failure(request, form, _PreparationFailed(status))
    plan_data = status.get("plan") or {}
    return render(
        request,
        "skeleton/web/platform/updates/preview.html",
        _context(
            area_of(plan_data.get("action") or source.get("action")),
            flow="review",
            plan_id=str(plan_id),
            plan_state=status["state"],
            plan=plan_data,
            summary=plan_summary(plan_data, _inventory()) if status["state"] == "ready" else None,
            theme_warnings=uninstall_theme_warnings(source),
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
    identifiers = {name: request.POST.get(name, "") for name in ("component_id", "version", "source_id")}
    back = AREAS[PLATFORM if identifiers["component_id"] == CORE_COMPONENT else APPS]["url_name"]
    try:
        if any(not value or len(value) > 128 for value in identifiers.values()):
            return HttpResponse(gettext("Identificação de release inválida."), status=400)
        target = request.POST.get("target", "")
        if identifiers["component_id"] == CORE_COMPONENT and target:
            refusal = _target_refusal(identifiers, target)
            if refusal is not None:
                messages.error(request, refusal)
                return redirect(back)
            request.session[TARGET_SESSION_KEY] = target
        elif identifiers["component_id"] == CORE_COMPONENT:
            request.session.pop(TARGET_SESSION_KEY, None)
        artifact = agent_client().fetch_release(**identifiers)
        return _fetched_plan(request, identifiers, artifact)
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
        return redirect(back)


def _fetched_plan(request, identifiers, artifact, message=None, *, automatic=True):
    """The plan of a fetched step, or the agent update it needs first (#773)."""
    digest, action = plan_artifact(artifact)
    blocking = _agent_behind(artifact)
    if blocking is not None:
        return _agent_first(request, artifact, blocking, identifiers=identifiers, artifacts=digest, automatic=automatic)
    if action is None and identifiers["component_id"] == CORE_COMPONENT:
        action = "core_update"
    elif action is None:
        action = "update" if identifiers["component_id"] in _installed_components() else "install"
    if message:
        messages.success(request, message)
    return _plan_page(
        request,
        PlanForm(initial={"action": action, "artifacts": digest}),
        artifact=artifact,
        agent_offer=_agent_offer(artifact),
    )


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
            PLATFORM,
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
    messages.success(request, gettext("Fonte salva. Ela passa a ser consultada em Verificar agora."))
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
    """Replace the agent with the one of an uploaded release package (#497); the agent restarts.

    The wait page then goes on to the core plan (#773): the fetched step again when the form names
    one, otherwise the plan of the core package the release carries.
    """
    digest = request.POST.get("release", "")
    if not DIGEST.fullmatch(digest):
        return HttpResponse(gettext("Digest de pacote inválido."), status=400)
    try:
        return _start_agent_update(
            request,
            digest=digest,
            required=None,
            action="core_update",
            identifiers=_identifiers(request.POST),
            artifacts=request.POST.get("artifacts", ""),
        )
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    return redirect("platform:updates")


def _agent_progress(required):
    """Where the agent update stands: ``ready`` with this agent, and every alive node, on ``required``."""
    try:
        inventory = agent_client().inventory()
    except (ValueError, RuntimeError, OSError):
        return {"ready": False, "reachable": False, "current": None, "cluster": None, "problem": None}
    current = _text((inventory.get("agent") or {}).get("version"), 64)
    ready = _version_at_least(current, required)
    try:
        cluster = agent_client().cluster()
    except (ValueError, RuntimeError, OSError):
        cluster = {"mode": "single"}
    nodes, problem = None, None
    if cluster.get("mode") == "cluster":
        alive = [node for node in cluster.get("nodes") or () if node.get("alive")]
        behind = [node for node in alive if not _version_at_least(node.get("agent_version"), required)]
        ready = ready and not behind
        nodes = {
            "total": len(alive),
            "done": len(alive) - len(behind),
            "behind": [{"node_id": node.get("node_id"), "agent_version": node.get("agent_version")} for node in behind],
        }
        if cluster.get("problem"):
            problem = ERROR_TEXT.get(cluster["problem"]) or gettext(
                "O agente deste nó informou um problema (%(code)s); veja o log dele no servidor."
            ) % {"code": cluster["problem"]}
    return {"ready": ready, "reachable": True, "current": current, "cluster": nodes, "problem": problem}


@platform_page(capability="updates.operate", writes=True)
@require_safe
@recent_authentication_required(allow_get=True)
def agent_wait(request):
    """Wait for the agent a core step updated (#773), then show that step's plan; only reads the agent."""
    continuation = _continuation(request)
    if continuation is None:
        return redirect("platform:updates")
    required = continuation["required"]
    if timezone.now() - continuation["started_at"] > AGENT_WAIT_LIMIT:
        request.session.pop(AGENT_CONTINUATION_KEY, None)
        return render(
            request,
            "skeleton/web/platform/updates/agent-wait.html",
            _context(PLATFORM, flow="choose", wait_state="failed", required=required),
        )
    progress = _agent_progress(required)
    if not progress["ready"]:
        return render(
            request,
            "skeleton/web/platform/updates/agent-wait.html",
            _context(PLATFORM, flow="choose", wait_state="waiting", required=required, progress=progress),
        )
    request.session.pop(AGENT_CONTINUATION_KEY, None)
    message = gettext("Agente atualizado para %(version)s. Confira o impacto antes de aprovar.") % {"version": required}
    identifiers = continuation["identifiers"]
    if identifiers is None:
        messages.success(request, message)
        initial = {"artifacts": continuation["artifacts"] or "", "action": continuation["action"]}
        return _plan_page(request, PlanForm(initial=initial))
    try:
        # The package is already in uploads/: no download, and the new agent checks it again.
        artifact = agent_client().fetch_release(**identifiers)
        return _fetched_plan(request, identifiers, artifact, message, automatic=False)
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
        return redirect("platform:updates")


@platform_page(capability="updates.operate", writes=True)
@require_safe
def operations(request):
    """The history of one area (#822): open and executed operations, or the closed previews (#821)."""
    area = _area(request.GET.get("area"))
    closed = request.GET.get("estado") == "encerradas"
    rows = DeploymentOperation.objects.order_by("-created_at")
    rows = rows.filter(operation="core_update") if area == PLATFORM else rows.exclude(operation="core_update")
    rows = rows.filter(state__in=CLOSED_PREVIEW_STATES) if closed else rows.exclude(state__in=CLOSED_PREVIEW_STATES)
    base = reverse("platform:updates-operations")
    filters = [
        {"label": gettext("Em aberto e executadas"), "url": f"{base}?area={area}", "current": not closed},
        {
            "label": gettext("Descartadas, substituídas e vencidas"),
            "url": f"{base}?area={area}&estado=encerradas",
            "current": closed,
        },
    ]
    return render(
        request,
        "skeleton/web/platform/updates/operations.html",
        _context(area, "history", operations=_described(rows[:50]), filters=filters, closed=closed),
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
            messages.error(request, gettext("A confirmação não corresponde a este plano. Confira o plano de novo."))
            return redirect("platform:updates-operation", operation_id=row.pk)
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
            # Inside the shell, never a bare 409 (#821): the page says what changed.
            messages.error(
                request,
                gettext(
                    "O plano mudou ou a instalação mudou desde a prévia. Confira o estado abaixo e, "
                    "se ele venceu, prepare a atualização de novo."
                ),
            )
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
        return redirect("platform:updates-operation", operation_id=row.pk)
    status_url, stale, summary = None, False, None
    if row.state == "preview":
        # Only reads (#821): a plan of an older generation is shown as expired, never approvable.
        inventory = _inventory()
        generation = inventory.get("generation")
        stale = preview_is_stale(row, generation if type(generation) is int else None)
        summary = plan_summary(row.plan or {}, inventory or None)
    elif row.state not in CLOSED_PREVIEW_STATES:
        try:
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
    closed = row.state in CLOSED_PREVIEW_STATES
    error = ERROR_TEXT.get(row.error_code) if row.error_code else None
    row.target = describe_plan(row.plan)
    return render(
        request,
        "skeleton/web/platform/updates/operation.html",
        _context(
            area_of(row.operation),
            "history" if closed else None,
            flow=None if closed else "approve" if row.state == "preview" else "follow",
            next_steps=next_steps,
            operation=row,
            closed=closed,
            stale=stale,
            summary=summary,
            error_text=error,
            confirmation_form=ConfirmationForm(
                initial={"revision": row.revision, "plan_digest": row.plan_digest},
                rolling=(row.plan or {}).get("rollout_mode") == "rolling",
            ),
            status_url=status_url,
            can_retry=submission_can_retry(row),
        ),
    )


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["POST"])
@recent_authentication_required
def discard(request, operation_id):
    """Discard a plan awaiting approval (#821); it can be prepared again at any time."""
    row = get_object_or_404(DeploymentOperation.objects, pk=operation_id)
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        return HttpResponse(gettext("Revisão inválida."), status=400)
    try:
        discard_preview(user=request.user, operation_id=row.pk, revision=revision)
    except ConcurrentUpdate:
        messages.error(request, gettext("Este plano já foi aprovado, descartado ou substituído."))
        return redirect("platform:updates-operation", operation_id=row.pk)
    messages.success(request, gettext("Plano descartado. Você pode preparar a atualização de novo quando quiser."))
    return redirect(AREAS[area_of(row.operation)]["url_name"])


@platform_page(capability="updates.operate", writes=True)
@require_http_methods(["POST"])
@recent_authentication_required
def status_link(request, operation_id):
    """Open the independent follow-up channel of a plan (#823): the ticket is asked for on the click.

    The ticket is short-lived and private to this operation; it is never stored, and asking for it
    only when the operator opens it keeps it from expiring on a page left open.
    """
    row = get_object_or_404(DeploymentOperation.objects, pk=operation_id)
    try:
        url = agent_client().status_ticket(str(row.pk)).get("status_url")
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
        return redirect("platform:updates-operation", operation_id=row.pk)
    if not isinstance(url, str) or not re.fullmatch(r"https?://[^\s\"'<>]{1,2000}", url):
        messages.error(request, gettext("O agente não devolveu um endereço de acompanhamento válido."))
        return redirect("platform:updates-operation", operation_id=row.pk)
    return HttpResponseRedirect(url)
