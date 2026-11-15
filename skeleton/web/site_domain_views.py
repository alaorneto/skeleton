"""``/o/<org>/settings/domains/``: custom domains of the organization address (#488), administrator only."""

from __future__ import annotations

from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy import site_domains
from skeleton.tenancy.organization_settings import current_settings

from .views import tenant_page


class SiteDomainForm(forms.Form):
    domain = forms.CharField(
        label=gettext_lazy("Domínio"),
        max_length=253,
        help_text=gettext_lazy("Só o domínio, como portal.empresa.com.br."),
    )


def _require_feature():
    if not site_domains.custom_domains_enabled():
        raise Http404


def _page(request, organization_id, form, *, status=200, error=None):
    record = current_settings()
    settings_url = reverse("web:organization-settings", kwargs={"organization_id": organization_id})
    public = urlsplit((getattr(settings, "SKELETON_PUBLIC_URL", "") or "").strip())
    return render(
        request,
        "skeleton/web/site_domains.html",
        {
            "form": form,
            "organization_id": organization_id,
            "rows": list(site_domains.site_domains()),
            "prefix": site_domains.SITE_CHALLENGE_PREFIX,
            "platform_host": public.hostname or "",
            "site_enabled": bool(record is not None and record.site_enabled),
            "error": error,
            "breadcrumbs": [
                {"label": gettext("Configurações"), "url": settings_url},
                {"label": gettext("Domínios próprios")},
            ],
        },
        status=status,
    )


@tenant_page(admin=True)
@never_cache
@require_http_methods(["GET", "POST"])
def site_domain_list(request, organization_id):
    _require_feature()
    form = SiteDomainForm(request.POST if request.method == "POST" else None)
    if request.method != "POST":
        return _page(request, organization_id, form)
    if not form.is_valid():
        return _page(request, organization_id, form, status=400)
    try:
        with transaction.atomic():
            site_domains.add_site_domain(actor=request.user, domain=form.cleaned_data["domain"])
    except ValidationError as exc:
        for field, problems in exc.message_dict.items():
            form.add_error(field if field in form.fields else None, problems)
        return _page(request, organization_id, form, status=400)
    messages.success(request, gettext("Domínio cadastrado. Publique o registro TXT e verifique."))
    return redirect("web:site-domains", organization_id=organization_id)


@tenant_page(admin=True)
@never_cache
@require_POST
def site_domain_action(request, organization_id, sysid):
    _require_feature()
    action = request.POST.get("action")
    if action not in ("verify", "revoke"):
        return _page(request, organization_id, SiteDomainForm(), status=400, error=gettext("Ação inválida."))
    try:
        with transaction.atomic():
            if action == "verify":
                record = site_domains.verify_site_domain(actor=request.user, domain_id=sysid)
            else:
                try:
                    revision = int(request.POST.get("revision", ""))
                except ValueError:
                    revision = None
                site_domains.revoke_site_domain(actor=request.user, domain_id=sysid, expected_revision=revision)
                record = None
    except site_domains.DomainUnavailable:
        error = gettext("Não foi possível verificar este domínio para esta organização.")
        return _page(request, organization_id, SiteDomainForm(), status=409, error=error)
    except ConcurrentUpdate:
        error = gettext("O domínio mudou. Atualize e tente de novo.")
        return _page(request, organization_id, SiteDomainForm(), status=409, error=error)
    if record is None:
        messages.success(request, gettext("Domínio removido."))
    elif record.status == "verified":
        messages.success(request, gettext("%(domain)s verificado.") % {"domain": record.domain})
    else:
        messages.warning(
            request,
            gettext("O registro TXT de %(domain)s não foi encontrado (%(code)s).")
            % {"domain": record.domain, "code": record.last_error_code},
        )
    return redirect("web:site-domains", organization_id=organization_id)


def site_host_check(request):
    """``/health/site-host/?domain=<host>`` (#489): may the proxy issue a certificate for this host?

    Answers only on ``SKELETON_HEALTH_HOST`` (the proxy's internal call) and only yes (200, empty)
    or no (404): an enabled organization address, by slug or by verified custom domain.
    """
    from django.http import HttpResponse, HttpResponseNotFound
    from django.http.request import split_domain_port

    from skeleton.tenancy.site import classify_host, find_site, site_of

    health = (getattr(settings, "SKELETON_HEALTH_HOST", "") or "").lower()
    if request.method not in ("GET", "HEAD") or not health:
        return HttpResponseNotFound()
    if split_domain_port(request.get_host().lower())[0] != split_domain_port(health)[0]:
        return HttpResponseNotFound()
    domain = (request.GET.get("domain") or "").strip().lower()
    kind = classify_host(domain) if domain else None
    if kind is None or kind.kind != "organization":
        return HttpResponseNotFound()
    site = find_site(kind.label) if kind.label else site_of(kind.organization_id)
    return HttpResponse(status=200) if site is not None else HttpResponseNotFound()
