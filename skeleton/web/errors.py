"""The operation error page: a title by status, the detail and the right way back (#473).

Views never render ``skeleton/web/error.html`` directly: ``error_response`` picks the title
from the status and offers "Voltar" (or "Voltar e recarregar" on a conflict) only to a page
of this same host, taken from the view or from a validated ``Referer``; never a redirect.
"""

from __future__ import annotations

from django.shortcuts import render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext

TEMPLATE = "skeleton/web/error.html"


def _title(status: int) -> str:
    titles = {
        400: gettext("Não foi possível concluir"),
        403: gettext("Acesso não permitido"),
        404: gettext("Página não encontrada"),
        409: gettext("Os dados mudaram"),
        413: gettext("Arquivo grande demais"),
        429: gettext("Muitas tentativas"),
    }
    return titles.get(status, gettext("Não foi possível concluir"))


def safe_referer(request) -> str | None:
    """The ``Referer`` when it points to this host (no open redirect); ``None`` otherwise."""
    referer = request.META.get("HTTP_REFERER") or ""
    if referer and url_has_allowed_host_and_scheme(
        referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return referer
    return None


def error_response(request, detail, *, status, back_url=None, back_label=None):
    """The operation error page: a title by status, the detail and a way back."""
    back_url = back_url or safe_referer(request)
    if back_url and not back_label:
        back_label = gettext("Voltar e recarregar") if status == 409 else gettext("Voltar")
    context = {
        "detail": detail,
        "title": _title(status),
        "back_url": back_url,
        "back_label": back_label,
        "status": status,
    }
    return render(request, TEMPLATE, context, status=status)
