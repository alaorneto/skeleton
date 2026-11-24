"""Display filters of the core templates: dates and times as DD/MM/AAAA HH:mm (#468).

``{{ value|moment }}`` renders ``<time datetime="ISO">15/11/2026 14:30</time>`` in the active
time zone (the person's, the organization's or the instance's, activated by the locale
middleware); ``|moment:"date"`` and ``|moment:"time"`` show only one part. Never print a date
raw in a template: the guard in ``tests/unit/test_template_dates.py`` refuses it.
"""

from __future__ import annotations

from datetime import date, datetime, time

from django import template
from django.utils import formats, timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from skeleton.web.vocabulary import app_markers, app_states

register = template.Library()
EMPTY = "—"


@register.filter(is_safe=True)
def moment(value, kind="datetime"):
    """<time> with DD/MM/AAAA HH:mm in the active time zone; "—" for empty values."""
    if value is None or value == "":
        return EMPTY
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value  # Not a date: autoescaped by the template like any text.
    if not isinstance(value, (date, time)):
        return value
    iso = value.isoformat()  # The instant as stored; the text below is in the active time zone.
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        if kind == "date":
            text = formats.date_format(value, "DATE_FORMAT")
        elif kind == "time":
            text = formats.time_format(value, "TIME_FORMAT")
        else:
            text = formats.date_format(value, "DATETIME_FORMAT")
    elif isinstance(value, date):
        text = formats.date_format(value, "DATE_FORMAT")
    else:
        text = formats.time_format(value, "TIME_FORMAT")
    return format_html('<time datetime="{}">{}</time>', iso, text)


# State code -> (label, tone) for every *Status/*State TextChoices of the core (#469). Tone rule:
# done/active/paid/delivered -> success; running/queued/waiting -> brand; needs action/late/trial ->
# warning; failed/dead/refused/suspended -> danger; cancelled/expired/draft/archived -> neutral.
# The same code with another meaning in a domain has a "<domain>:<code>" entry; pass the domain
# (or an explicit label) as the filter argument: {{ task.status|status_badge:"tasks" }}.
STATUS = {
    "pending": (_("Pendente"), "warning"),
    "awaiting_verification": (_("Aguardando confirmação do email"), "warning"),
    "approved": (_("Aprovado"), "success"),
    "rejected": (_("Recusado"), "danger"),
    "withdrawn": (_("Retirado"), "neutral"),
    "expired": (_("Expirado"), "neutral"),
    "verified": (_("Verificado"), "success"),
    "failed": (_("Falhou"), "danger"),
    "queued": (_("Na fila"), "brand"),
    "running": (_("Em andamento"), "brand"),
    "succeeded": (_("Concluído"), "success"),
    "completed": (_("Concluído"), "success"),
    "dead": (_("Esgotado"), "danger"),
    "cancelled": (_("Cancelado"), "neutral"),
    "canceled": (_("Cancelado"), "neutral"),
    "retrying": (_("Nova tentativa agendada"), "warning"),
    "sent": (_("Enviada"), "success"),
    "skipped": (_("Ignorada"), "neutral"),
    "available": (_("Disponível"), "success"),
    "quarantined": (_("Em quarentena"), "danger"),
    "purged": (_("Expurgado"), "neutral"),
    "unauthorized": (_("Sem acesso ao registro"), "danger"),
    "ready": (_("Pronto"), "success"),
    "previewed": (_("Prévia pronta"), "brand"),
    "applying": (_("Aplicando"), "brand"),
    "applied": (_("Aplicado"), "success"),
    "suspended": (_("Suspensa"), "danger"),
    "active": (_("Ativo"), "success"),
    "waiting": (_("Aguardando ramos"), "brand"),
    "open": (_("Aberta"), "brand"),
    "past_due": (_("Em atraso"), "warning"),
    "restricted": (_("Somente leitura"), "warning"),
    "trialing": (_("Em avaliação"), "warning"),
    "incomplete": (_("Aguardando pagamento"), "warning"),
    "ended": (_("Encerrado"), "neutral"),
    "paid": (_("Paga"), "success"),
    "void": (_("Anulada"), "neutral"),
    "uncollectible": (_("Incobrável"), "danger"),
    "refunded": (_("Estornado"), "neutral"),
    "created": (_("Criada"), "neutral"),
    "received": (_("Recebido"), "brand"),
    "processed": (_("Processado"), "success"),
    "ignored": (_("Ignorado"), "neutral"),
    "confirmed": (_("Confirmado"), "success"),
    "tentative": (_("Provisório"), "warning"),
    "draft": (_("Rascunho"), "neutral"),
    "archived": (_("Arquivado"), "neutral"),
    "inactive": (_("Inativo"), "neutral"),
    "read": (_("Lida"), "neutral"),
    "unread": (_("Não lida"), "warning"),
    "export": (_("Exportação"), "brand"),
    "import": (_("Importação"), "brand"),
    # Audit operations (AuditEvent.Operation).
    "create": (_("Criação"), "success"),
    "update": (_("Alteração"), "brand"),
    "soft_delete": (_("Exclusão"), "danger"),
    "restore": (_("Restauração"), "warning"),
    "hard_delete": (_("Exclusão física"), "danger"),
    # Jobs.
    "jobs:running": (_("Executando"), "brand"),
    # Notification types turned on or off by the platform or the organization (#784, #785).
    "enabled": (_("Ligado"), "success"),
    "notifications:enabled": (_("Ligado"), "success"),
    "notifications:disabled": (_("Desligado"), "neutral"),
    # Announcements of the platform (#789): masculine in Portuguese.
    "announcement:sent": (_("Enviado"), "success"),
    # Notification and webhook deliveries.
    "deliveries:pending": (_("Na fila"), "brand"),
    "deliveries:succeeded": (_("Entregue"), "success"),
    "deliveries:dead": (_("Falhou"), "danger"),
    # Attachments and verified domains.
    "files:pending": (_("Aguardando verificação"), "warning"),
    "files:rejected": (_("Rejeitado"), "danger"),
    "domains:pending": (_("Aguardando verificação"), "warning"),
    # Join and erasure requests.
    "requests:pending": (_("Aguardando aprovação"), "warning"),
    "requests:cancelled": (_("Retirado"), "neutral"),
    # Flow instances, steps and tasks (feminine in Portuguese).
    "flows:active": (_("Ativa"), "brand"),
    "flows:completed": (_("Concluída"), "success"),
    "flows:cancelled": (_("Cancelada"), "neutral"),
    "flows:failed": (_("Com falha"), "danger"),
    "flows:open": (_("Aberta"), "brand"),
    "assigned": (_("Atribuída"), "warning"),
    "tasks:assigned": (_("Aberta"), "warning"),
    "tasks:queued": (_("Na fila"), "neutral"),
    "tasks:completed": (_("Concluída"), "success"),
    "tasks:cancelled": (_("Cancelada"), "neutral"),
    # Billing.
    "billing:active": (_("Em dia"), "success"),
    "subscription:active": (_("Ativa"), "success"),
    "subscription:canceled": (_("Cancelada"), "neutral"),
    "invoice:open": (_("Em aberto"), "warning"),
    "payment:succeeded": (_("Confirmado"), "success"),
    "payment:failed": (_("Recusado"), "danger"),
    "item:pending": (_("Aguardando pagamento"), "warning"),
    "checkout:open": (_("Aberta"), "brand"),
    # Lifecycle of records and settings shown across the core screens (#767).
    "deleted": (_("Excluído"), "neutral"),
    "reversed": (_("Estornado"), "neutral"),
    "revoked": (_("Revogado"), "neutral"),
    "disabled": (_("Desativado"), "neutral"),
    "published": (_("Publicada"), "success"),
    "unpublished": (_("Não publicada"), "neutral"),
    "unavailable": (_("Indisponível"), "warning"),
    "over_limit": (_("Acima do limite"), "warning"),
    "alive": (_("Com sinal"), "success"),
    "no_signal": (_("Sem sinal"), "warning"),
    "done": (_("Feito"), "success"),
    "preview": (_("Prévia"), "brand"),
    "discarded": (_("Descartado"), "neutral"),
    "superseded": (_("Substituído"), "neutral"),
    "stale": (_("Vencido"), "neutral"),
    "interrupted": (_("Interrompido"), "danger"),
    "update_available": (_("Atualização disponível"), "brand"),
    "update_blocked": (_("Atualização bloqueada"), "warning"),
    "up_to_date": (_("Atualizado"), "success"),
    "current": (_("Atual"), "neutral"),
    "incompatible": (_("Incompatível"), "warning"),
    "restored": (_("Revertida"), "warning"),
    "submission_failed": (_("Envio falhou"), "danger"),
    "installed": (_("Instalado"), "success"),
    "bundled": (_("Incluído no núcleo"), "success"),
    "project-local": (_("Local"), "success"),
    "schema_retained": (_("Dados preservados"), "warning"),
    "not_contracted": (_("Não contratada"), "warning"),
    "all": (_("Todas"), "success"),
    "selected": (_("Selecionadas"), "brand"),
    "none": (_("Nenhuma"), "neutral"),
    # Apps of an organization.
    "apps:active": (_("Ativa"), "success"),
    "apps:disabled": (_("Desativada"), "neutral"),
    "apps:not_contracted": (_("Não contratada"), "warning"),
    "apps:unavailable": (_("Indisponibilizada pela plataforma"), "warning"),
    # Availability of an installed app to the organizations.
    "availability:all": (_("Todas as organizações"), "success"),
    "availability:selected": (_("Organizações selecionadas"), "brand"),
    "availability:none": (_("Indisponível"), "neutral"),
    # Setup steps.
    "setup:done": (_("Feito"), "success"),
    "setup:skipped": (_("Depois"), "warning"),
    "setup:pending": (_("Pendente"), "neutral"),
    # Deployment operations and installed components.
    # A plan awaiting approval asks for an action (#821); closed previews never run.
    "updates:preview": (_("Aguardando aprovação"), "warning"),
    "updates:discarded": (_("Descartada"), "neutral"),
    "updates:superseded": (_("Substituída"), "neutral"),
    "updates:stale": (_("Vencida"), "neutral"),
    "updates:approved": (_("Aprovada"), "brand"),
    "updates:queued": (_("Na fila"), "brand"),
    "updates:running": (_("Em andamento"), "brand"),
    "updates:interrupted": (_("Interrompida"), "danger"),
    "updates:cancelled": (_("Cancelada"), "neutral"),
    # The update situation of a component on the overview of an area (#822).
    "updates:update_available": (_("Atualização disponível"), "brand"),
    "updates:update_blocked": (_("Atualização bloqueada"), "warning"),
    "updates:up_to_date": (_("Atualizado"), "success"),
    "updates:submission_failed": (_("Envio falhou"), "danger"),
    "updates:succeeded": (_("Concluída"), "success"),
    "updates:completed": (_("Concluída"), "success"),
    "updates:restored": (_("Revertida"), "warning"),
    "updates:failed": (_("Falhou"), "danger"),
    "components:installed": (_("Instalado"), "success"),
    "components:project-local": (_("Local"), "success"),
    "components:bundled": (_("Incluído no núcleo"), "success"),
    "components:schema_retained": (_("Dados preservados"), "warning"),
    "releases:available": (_("Disponível"), "brand"),
    "releases:current": (_("Já instalada"), "neutral"),
    "releases:incompatible": (_("Incompatível"), "warning"),
    "releases:installed": (_("Instalada"), "success"),
    "sources:available": (_("Respondendo"), "success"),
    # Rules of the platform for new organizations.
    "policy:ignored": (_("Configuração indisponível: ignorada"), "warning"),
}
DOMAINS = {key.split(":", 1)[0] for key in STATUS if ":" in key}

# Marker code -> (label, tone): a fixed property that changes behaviour or access (#767). A marker
# is a pill without the dot; a state (status_badge) keeps the dot. The argument replaces the label
# when the text depends on data: {{ "managed"|marker:label }}.
MARKERS = {
    "mandatory": (_("Obrigatório"), "brand"),
    "read_only": (_("Somente leitura"), "warning"),
    "secret": (_("Segredo"), "neutral"),
    "platform_admin": (_("Administrador da plataforma"), "brand"),
    "partial_admin": (_("Acesso parcial"), "warning"),
    "default": (_("Padrão"), "neutral"),
    "managed": (_("Gerenciado por outra app"), "neutral"),
    "resend": (_("Reenvio"), "neutral"),
    "returning": (_("Já foi membro: aprovar reativa o vínculo"), "warning"),
    "cancel_at_period_end": (_("Cancela no fim do período"), "warning"),
    "hidden_from_login": (_("Fora da tela de login"), "neutral"),
    "via_group": (_("Via grupo"), "neutral"),
    "fixed": (_("Fixo"), "neutral"),
    "locked": (_("Travada"), "warning"),
    "primary": (_("Primário"), "brand"),
    "private": (_("Privado"), "neutral"),
    "primary_key": (_("Chave primária"), "brand"),
    "nullable": (_("Aceita vazio"), "neutral"),
    "sensitive": (_("Sensível"), "warning"),
    "personal": (_("Pessoal"), "warning"),
    # The kind of an installed app (#807); a library has no availability of its own: it is
    # active while an app that depends on it is (#792).
    "app": (_("Aplicativo"), "neutral"),
    "library": (_("Biblioteca"), "neutral"),
    # A notification type the organization cannot change: the platform's or locked by it (#785).
    "platform_defined": (_("Definido pela plataforma"), "neutral"),
}

# Code -> translated text of a value or category shown as plain text (#767): never a pill.
CODE_LABELS = {
    "table_origin": {
        "core": _("Núcleo"),
        "application": _("Aplicativo"),
        "framework": _("Django"),
        "third_party": _("Biblioteca de terceiros"),
        "unclassified": _("Não classificada"),
    },
    "table_scope": {"organization": _("Organização"), "global": _("Global"), "technical": _("Técnica")},
    "audit_mode": {
        "append_only": _("Somente acréscimo"),
        "operational": _("Operacional"),
        "trigger": _("Por gatilho"),
        "library": _("Da biblioteca"),
        "none": _("Sem auditoria"),
    },
    "app_origin": {
        "bundled": _("Incluído no núcleo"),
        "project-local": _("Do projeto"),
        "installed": _("Instalado por pacote"),
    },
    "setting_origin": {"env": _("Ambiente"), "instance": _("Configuração da instância"), "default": _("Padrão")},
    # Stages of a deployment operation, as the agent reports them (#823).
    "update_stage": {
        "preview": _("Aguardando aprovação"),
        "discarded": _("Descartada pelo operador"),
        "superseded": _("Substituída por um plano mais novo"),
        "stale": _("Vencida: a instalação mudou"),
        "awaiting_agent": _("Enviando ao agente"),
        "awaiting_submission": _("Enviando ao agente"),
        "approved": _("Aprovada, na fila do agente"),
        "cluster_preparing": _("Preparando os nós"),
        "cluster_drain": _("Parando os nós"),
        "maintenance_entering": _("Entrando em manutenção"),
        "drained": _("Tráfego parado"),
        "backup_ready": _("Backup feito"),
        "migration_started": _("Aplicando as migrations"),
        "migrated": _("Migrations aplicadas"),
        "runtime_checked": _("Ambiente conferido"),
        "rolling_started": _("Trocando nó a nó"),
        "cutover_started": _("Trocando a versão"),
        "release_switched": _("Versão trocada"),
        "workers_started": _("Serviços iniciados"),
        "health_verified": _("Saúde conferida"),
        "committed": _("Concluída"),
        "traffic_release_started": _("Liberando o tráfego"),
        "ready": _("Concluída e no ar"),
        "failed": _("Falhou"),
    },
}


@register.filter(is_safe=True)
def status_badge(code, label=None):
    """A translated, coloured badge for a state code; unknown codes render neutral.

    The argument is a domain of the map (``"flows"``, ``"invoice"``...), a domain an app registered
    (``"med_scheduling.appointment"``, #833) or a label that replaces the one of the map
    (``{{ job.state|status_badge:job.get_state_display }}``). Never raises.
    """
    if code is None or code == "":
        return EMPTY
    key = str(code)
    states = app_states(str(label)) if label is not None else None
    if states is not None:  # An app's domain stands alone: an unknown code is neutral, never the core's.
        text, tone = states.get(key) or (key, "neutral")
        return format_html('<span class="badge badge-{} badge-dot">{}</span>', tone, text)
    domain = str(label) if label is not None and str(label) in DOMAINS else None
    text, tone = STATUS.get(f"{domain}:{key}") or STATUS.get(key) or (key, "neutral")
    if label is not None and domain is None:
        text = label
    return format_html('<span class="badge badge-{} badge-dot">{}</span>', tone, text)


@register.filter(is_safe=True)
def marker(code, label=None):
    """A pill without the dot for a fixed property (#767); unknown codes render neutral.

    The argument is a domain an app registered (``"med_scheduling.appointment"``, #833) or a label
    that replaces the one of the map (``{{ "managed"|marker:label }}``).
    """
    if code is None or code == "":
        return ""
    markers = app_markers(str(label)) if label is not None else None
    if markers is not None:
        text, tone = markers.get(str(code)) or (str(code), "neutral")
        return format_html('<span class="badge badge-{}">{}</span>', tone, text)
    text, tone = MARKERS.get(str(code)) or (str(code), "neutral")
    return format_html('<span class="badge badge-{}">{}</span>', tone, text if label is None else label)


@register.filter(name="concept_icon")
def concept_icon_filter(concept):
    """``with name="billing"|concept_icon``: the one icon of a concept (#768)."""
    from skeleton.web.icons import concept_icon

    return concept_icon(concept)


@register.filter
def code_label(code, domain):
    """``{{ row.origin|code_label:"table_origin" }}``: the translated text of a code, as plain text."""
    if code is None or code == "":
        return EMPTY
    return CODE_LABELS.get(str(domain), {}).get(str(code), str(code))


def _required(field) -> bool:
    """A form field counts as required when Django requires it or the page marks it (#474)."""
    attrs = getattr(getattr(field, "widget", None), "attrs", {}) or {}
    return bool(
        getattr(field, "required", False) or getattr(field, "marked_required", False) or "data-required" in attrs
    )


@register.filter
def is_required(bound_field) -> bool:
    """``{% if field|is_required %}``: the * mark of components/form-field.html."""
    return _required(getattr(bound_field, "field", None))


@register.filter
def any_required(form) -> bool:
    """Whether a form shows "Campos com * são obrigatórios." above its fields."""
    fields = getattr(form, "visible_fields", lambda: [])()
    return any(
        _required(bound.field) and getattr(bound.field.widget, "input_type", "") != "checkbox" for bound in fields
    )


@register.filter
def person(user_id, people):
    """``{{ row.actor_sysid|person:people }}``: the member's name, or "Pessoa indisponível" (#475).

    ``people`` comes from ``skeleton.web.people.people_labels`` (one lookup per page) and only
    holds active members of the current organization.
    """
    if not user_id:
        return _("Sistema")
    key = str(user_id)
    name = (people or {}).get(key)
    if name:
        return format_html('<span title="{}">{}</span>', key, name)
    return format_html(
        '<span class="text-body-subtle">{}</span> <code class="uuid" title="{}">{}</code>',
        _("Pessoa indisponível"),
        key,
        key[:8],
    )
