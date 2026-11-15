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


TONES = {"success", "warning", "danger", "brand", "neutral"}
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
}
DOMAINS = {key.split(":", 1)[0] for key in STATUS if ":" in key}


@register.filter(is_safe=True)
def status_badge(code, label=None):
    """A translated, coloured badge for a state code; unknown codes render neutral.

    The argument is a domain of the map (``"flows"``, ``"invoice"``...) or a label that replaces
    the one of the map (``{{ job.state|status_badge:job.get_state_display }}``). Never raises.
    """
    if code is None or code == "":
        return EMPTY
    key = str(code)
    domain = str(label) if label is not None and str(label) in DOMAINS else None
    text, tone = STATUS.get(f"{domain}:{key}") or STATUS.get(key) or (key, "neutral")
    if label is not None and domain is None:
        text = label
    return format_html('<span class="badge badge-{} badge-dot">{}</span>', tone, text)
