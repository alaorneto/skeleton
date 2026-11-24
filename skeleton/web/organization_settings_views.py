"""``/o/<org>/settings/``: the administrator edits the organization's defaults and brand (#167, #168).

The logo route serves the image inline only to members of the organization (the tenant
middleware answers 404 to anyone else), with private caching and ``nosniff``.
"""

from __future__ import annotations

from zoneinfo import available_timezones

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.branding import open_logo, remove_logo, replace_logo
from skeleton.tenancy.organization_settings import (
    CALENDAR_FIELDS,
    WRITE_FIELDS,
    current_settings,
    revision_of,
    update_settings,
)

from .views import tenant_page


def _timezone_choices() -> list[tuple[str, str]]:
    return [("", gettext("Padrão da instância"))] + [(zone, zone) for zone in sorted(available_timezones())]


class OrganizationSettingsForm(forms.Form):
    display_name = forms.CharField(
        label=gettext_lazy("Nome de exibição"),
        max_length=200,
        required=False,
        help_text=gettext_lazy("Aparece no menu e nos emails. Em branco, vale o nome da organização."),
    )
    default_language = forms.ChoiceField(
        label=gettext_lazy("Idioma padrão"),
        required=False,
        help_text=gettext_lazy("Vale para quem não escolheu um idioma no perfil."),
    )
    default_timezone = forms.ChoiceField(
        label=gettext_lazy("Fuso horário padrão"),
        required=False,
        help_text=gettext_lazy("Vale para quem não escolheu um fuso no perfil."),
    )
    brand_color = forms.CharField(
        label=gettext_lazy("Cor da marca"),
        max_length=7,
        required=False,
        help_text=gettext_lazy(
            "Formato #RRGGBB, com contraste de pelo menos 4,5:1 com o branco. Em branco, vale a cor padrão."
        ),
        widget=forms.TextInput(attrs={"placeholder": "#1d4ed8", "autocomplete": "off"}),
    )
    business_hours = forms.CharField(
        label=gettext_lazy("Expediente"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 5, "placeholder": "seg 09:00-12:00 13:00-18:00"}),
        help_text=gettext_lazy(
            "Uma linha por dia (seg, ter, qua, qui, sex, sab, dom) com os intervalos. Usado nos prazos em horas úteis."
        ),
    )
    holidays = forms.CharField(
        label=gettext_lazy("Feriados"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "2026-12-25"}),
        help_text=gettext_lazy("Uma data por linha, no formato AAAA-MM-DD."),
    )
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_language"].choices = [("", gettext("Padrão da instância")), *settings.LANGUAGES]
        self.fields["default_timezone"].choices = _timezone_choices()


class SiteAddressForm(forms.Form):
    """The organization's own address (#480), shown only when the instance and the slug allow it."""

    site_enabled = forms.BooleanField(label=gettext_lazy("Ativar endereço próprio"), required=False)


class OrganizationSlugForm(forms.Form):
    """The organization's identifier (#765): its own form, with the organization's revision."""

    slug = forms.SlugField(
        label=gettext_lazy("Identificador"),
        max_length=63,
        help_text=gettext_lazy("Letras minúsculas, dígitos e hífen. Aparece no endereço próprio da organização."),
    )
    expected_revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def __init__(self, *args, organization, **kwargs):
        from .slug_check import slug_check_attrs

        kwargs.setdefault("initial", {"slug": organization.slug, "expected_revision": organization.revision})
        super().__init__(*args, **kwargs)
        slug_check_attrs(
            self.fields["slug"],
            url=reverse("web:organization-slug-check", kwargs={"organization_id": organization.pk}),
            current=organization.slug,
        )


def _organization(organization_id):
    from skeleton.tenancy.models import Organization

    return Organization.objects.get(pk=organization_id)


def _site_address(organization_id):
    """``(origin, refusal)``: the address when available, else why the section offers no checkbox."""
    from skeleton.tenancy.models import Organization
    from skeleton.tenancy.organization_settings import site_refusal
    from skeleton.tenancy.site import base_domain, organization_origin

    if not base_domain():
        return None, gettext("Esta instalação não oferece endereço próprio para organizações.")
    organization = Organization.objects.get(pk=organization_id)
    if site_refusal(organization):
        return None, gettext("O identificador desta organização não pode ser usado como endereço.")
    return organization_origin(organization.slug), ""


def _custom_domains_enabled() -> bool:
    from skeleton.tenancy.site_domains import custom_domains_enabled

    return custom_domains_enabled()


LOCKED_HELP = gettext_lazy("Definido pela plataforma.")


def lock_fields(form, locked, data=None):
    """Show the fields the platform locked (#701) as read-only and keep their value in the POST.

    A disabled field is not sent by the browser: its displayed value is put back before the
    form validates, so the service receives the locked value. A forged different value still
    reaches the service, which refuses it with ``setting_locked``.
    """
    for name in locked:
        field = form.fields.get(name)
        if field is None:
            continue
        field.widget.attrs["disabled"] = True
        field.help_text = LOCKED_HELP
        if data is not None and name not in data:
            value = form.initial.get(name, field.initial)
            if isinstance(field, forms.BooleanField):
                if value:
                    data[name] = "on"
            else:
                data[name] = "" if value is None else value
    return form


def _locked():
    from skeleton.tenancy.setting_locks import locked_fields

    return set(locked_fields("organization"))


def _apply_error(form: OrganizationSettingsForm, error: ValidationError, site_form=None) -> None:
    for field, problems in error.message_dict.items():
        if site_form is not None and field in site_form.fields:
            site_form.add_error(field, problems)
        else:
            form.add_error(field if field in form.fields else None, problems)


def _page(
    request, organization_id, form, *, status=200, logo_errors=(), site_form=None, icon_errors=(), slug_form=None
):
    record = current_settings()
    if slug_form is None:
        slug_form = OrganizationSlugForm(organization=_organization(organization_id))
    dashboard = reverse("web:dashboard", kwargs={"organization_id": organization_id})
    origin, site_refused = _site_address(organization_id)
    locked = _locked()
    if site_form is None:
        site_form = SiteAddressForm(initial={"site_enabled": bool(record is not None and record.site_enabled)})
    lock_fields(form, locked)
    lock_fields(site_form, locked)
    return render(
        request,
        "skeleton/web/organization_settings.html",
        {
            "form": form,
            "slug_form": slug_form,
            "site_form": site_form if origin else None,
            "site_origin": origin,
            "site_refused": site_refused,
            "custom_domains": _custom_domains_enabled(),
            "organization_id": organization_id,
            "revision": revision_of(record),
            "has_logo": bool(record is not None and record.logo_key),
            "logo_errors": list(logo_errors),
            "has_icon": bool(record is not None and record.icon_key),
            "logo_locked": "logo" in locked,
            "icon_locked": "icon" in locked,
            "icon_errors": list(icon_errors),
            "cancel_url": dashboard,
            "breadcrumbs": [{"label": gettext("Início"), "url": dashboard}, {"label": gettext("Configurações")}],
        },
        status=status,
    )


def _initial(record):
    from skeleton.tenancy.business_hours import format_holidays, format_hours

    initial = {name: getattr(record, name, "") for name in WRITE_FIELDS}
    initial["business_hours"] = format_hours(getattr(record, "business_hours", None))
    initial["holidays"] = format_holidays(getattr(record, "holidays", None))
    initial["revision"] = revision_of(record)
    return initial


def _form(request, record):
    initial = _initial(record)
    if request.method != "POST":
        return OrganizationSettingsForm(None, initial=initial)
    data = request.POST.copy()
    form = OrganizationSettingsForm(data, initial=initial)
    return lock_fields(form, _locked(), data)


@tenant_page(admin=True)
@never_cache
@require_http_methods(["GET", "POST"])
def organization_settings(request, organization_id):
    record = current_settings()
    form = _form(request, record)
    origin, _refused = _site_address(organization_id)
    site_form = None
    if request.method == "POST" and origin:
        site_data = request.POST.copy()
        site_form = lock_fields(
            SiteAddressForm(site_data, initial={"site_enabled": bool(record is not None and record.site_enabled)}),
            _locked(),
            site_data,
        )
    status = 200
    if request.method == "POST":
        allowed = set(form.fields) | set(SiteAddressForm.base_fields) | {"csrfmiddlewaretoken"}
        if set(request.POST) - allowed:
            form.add_error(None, gettext("Campo não permitido."))
            status = 400
        elif form.is_valid() and (site_form is None or site_form.is_valid()):
            data = {name: form.cleaned_data[name] for name in (*WRITE_FIELDS, *CALENDAR_FIELDS)}
            if site_form is not None:
                # Only offered when the address is available; otherwise the stored choice is kept.
                data["site_enabled"] = site_form.cleaned_data["site_enabled"]
            elif "site_enabled" in request.POST:
                data["site_enabled"] = True  # The service refuses it with the reason.
            try:
                with transaction.atomic():
                    update_settings(actor=request.user, data=data, expected_revision=form.cleaned_data["revision"])
            except ConcurrentUpdate:
                form.add_error(None, gettext("As configurações mudaram. Atualize a página antes de tentar novamente."))
                status = 409
            except ValidationError as exc:
                from skeleton.service_api.organizations import is_setting_locked

                _apply_error(form, exc, site_form)
                status = 409 if is_setting_locked(exc) else 400
            else:
                messages.success(request, gettext("Configurações salvas."))
                return redirect("web:organization-settings", organization_id=organization_id)
        else:
            status = 400
    return _page(request, organization_id, form, status=status, site_form=site_form)


@tenant_page(admin=True)
@never_cache
@require_POST
def organization_slug_update(request, organization_id):
    """The administrator changes the organization's identifier (#765); the service checks it again."""
    from skeleton.tenancy.slugs import change_own_slug

    organization = _organization(organization_id)
    slug_form = OrganizationSlugForm(request.POST, organization=organization)
    status = 400
    if slug_form.is_valid():
        try:
            with transaction.atomic():
                organization = change_own_slug(
                    actor=request.user,
                    slug=slug_form.cleaned_data["slug"],
                    expected_revision=slug_form.cleaned_data["expected_revision"],
                )
        except ConcurrentUpdate:
            slug_form.add_error(None, gettext("A organização mudou. Atualize a página antes de tentar novamente."))
            status = 409
        except ValidationError as exc:
            _apply_error(slug_form, exc)
        else:
            messages.success(request, gettext("Identificador atualizado."))
            return redirect(_settings_url(request, organization))
    form = OrganizationSettingsForm(initial=_initial(current_settings()))
    return _page(request, organization_id, form, status=status, slug_form=slug_form)


def _settings_url(request, organization):
    """The settings page; on the organization's own slug host, at the address of the new slug."""
    from skeleton.tenancy.routing import site_organization_id
    from skeleton.tenancy.site import classify_host, organization_origin

    path = reverse("web:organization-settings", kwargs={"organization_id": organization.pk})
    if site_organization_id() == organization.pk and classify_host(request.get_host()).kind == "organization":
        origin = organization_origin(organization.slug)
        if origin:
            return origin + path
    return path


@tenant_page(admin=True)
@require_safe
def organization_slug_check(request, organization_id):
    """Whether the typed identifier is free for this organization (#765); only for its administrator."""
    from .slug_check import slug_check_response

    return slug_check_response(request, organization_id=organization_id)


@tenant_page(admin=True)
@never_cache
@require_POST
def organization_logo_update(request, organization_id):
    action = request.POST.get("action")
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = -1
    form = OrganizationSettingsForm(initial={"revision": revision_of(current_settings())})
    try:
        with transaction.atomic():
            if action == "upload" and "logo" in request.FILES:
                replace_logo(actor=request.user, upload=request.FILES["logo"], expected_revision=revision)
            elif action == "remove":
                remove_logo(actor=request.user, expected_revision=revision)
            else:
                raise ValidationError({"logo": gettext("Escolha um arquivo de imagem.")})
    except ConcurrentUpdate:
        return _page(
            request,
            organization_id,
            form,
            status=409,
            logo_errors=[gettext("As configurações mudaram. Atualize a página.")],
        )
    except ValidationError as exc:
        errors = [message for messages_ in exc.message_dict.values() for message in messages_]
        return _page(request, organization_id, form, status=400, logo_errors=errors)
    messages.success(request, gettext("Logo removido.") if action == "remove" else gettext("Logo atualizado."))
    return redirect("web:organization-settings", organization_id=organization_id)


@tenant_page()
@require_safe
def organization_logo(request, organization_id):
    from skeleton.files.storage import open_object

    found = open_logo()
    if found is None:
        raise Http404
    record, kind = found
    etag = '"' + record.logo_key.rsplit("/", 1)[1] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"}
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponseNotModified()
    else:
        # At most 512 KB: read inside the tenant context (streaming is refused there).
        with open_object(record.logo_key) as stream:
            response = HttpResponse(stream.read(), content_type=kind)
        response["Content-Disposition"] = "inline"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    for name, value in headers.items():
        response[name] = value
    return response


def public_site_image(request, found):
    """Bytes of a published brand image on the organization host: public cache, ``nosniff``, sandbox CSP."""
    if found is None:
        raise Http404
    content, kind = found
    response = HttpResponse(content, content_type=kind)
    response["Content-Disposition"] = "inline"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    response["Cache-Control"] = "public, max-age=300"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_safe
def site_logo(request):
    """``/site/logo/`` (#484): the logo of the organization host, public by the administrator's choice.

    Only on an organization host and only the key of the site index; 404 on the main domain.
    """
    from skeleton.tenancy.branding import open_site_logo

    site = getattr(request, "organization_site", None)
    if site is None or not site.logo_key:
        raise Http404
    return public_site_image(request, open_site_logo(site))


def _default_favicon():
    from django.templatetags.static import static

    return static("skeleton/web/favicon.svg")


def site_icon_url(site) -> str:
    return reverse("web:site-icon") + f"?v={site.icon_key[-12:]}"


@require_safe
def site_icon(request):
    """``/site/icon/`` (#485): the public PNG icon of the organization host; without one, the core favicon."""
    from django.shortcuts import redirect as go

    from skeleton.tenancy.branding import open_site_icon

    site = getattr(request, "organization_site", None)
    if site is None:
        raise Http404
    if not site.icon_key:
        return go(_default_favicon())
    return public_site_image(request, open_site_icon(site))


@require_safe
def site_manifest(request):
    """``/site/manifest.webmanifest`` (#485): only the name, colour and icon already public."""
    from django.http import JsonResponse

    site = getattr(request, "organization_site", None)
    if site is None:
        raise Http404
    manifest = {"name": site.name, "short_name": site.name[:12], "start_url": "/", "display": "standalone"}
    if site.brand_color:
        manifest["theme_color"] = site.brand_color
    if site.icon_key:
        manifest["icons"] = [{"src": reverse("web:site-icon"), "sizes": "512x512", "type": "image/png"}]
    response = JsonResponse(manifest, content_type="application/manifest+json")
    response["Cache-Control"] = "public, max-age=300"
    return response


@require_safe
def favicon(request):
    """``/favicon.ico``: the organization's icon on its host, the instance icon on the main domain, else the core SVG."""
    from django.shortcuts import redirect as go

    site = getattr(request, "organization_site", None)
    if site is not None and site.icon_key:
        return go(site_icon_url(site))
    if site is None:
        from skeleton.appearance.snapshot import current_appearance

        version = current_appearance().icon_version
        if version:  # The icon of the instance (uploaded or from the active theme package, #639).
            return go(f"/brand/icon/?v={version}")
    return go(_default_favicon())


@tenant_page()
@require_safe
def organization_icon(request, organization_id):
    """The icon of the organization for its members on the main domain (#485)."""
    from skeleton.tenancy.branding import open_icon

    found = open_icon()
    if found is None:
        raise Http404
    record, content = found
    etag = '"' + record.icon_key.rsplit("/", 1)[1] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"}
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(content, content_type="image/png")
        response["Content-Disposition"] = "inline"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    for name, value in headers.items():
        response[name] = value
    return response


@tenant_page(admin=True)
@never_cache
@require_POST
def organization_icon_update(request, organization_id):
    from skeleton.tenancy.branding import remove_icon, replace_icon

    action = request.POST.get("action")
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = -1
    form = OrganizationSettingsForm(initial={"revision": revision_of(current_settings())})
    try:
        with transaction.atomic():
            if action == "upload" and "icon" in request.FILES:
                replace_icon(actor=request.user, upload=request.FILES["icon"], expected_revision=revision)
            elif action == "remove":
                remove_icon(actor=request.user, expected_revision=revision)
            else:
                raise ValidationError({"icon": gettext("Escolha um arquivo PNG.")})
    except ConcurrentUpdate:
        return _page(
            request,
            organization_id,
            form,
            status=409,
            icon_errors=[gettext("As configurações mudaram. Atualize a página.")],
        )
    except ValidationError as exc:
        errors = [message for messages_ in exc.message_dict.values() for message in messages_]
        return _page(request, organization_id, form, status=400, icon_errors=errors)
    messages.success(request, gettext("Ícone removido.") if action == "remove" else gettext("Ícone atualizado."))
    return redirect("web:organization-settings", organization_id=organization_id)


class MFAPolicyForm(forms.Form):
    """Who must enrol MFA in the organization (#740); only tightens the platform's rules."""

    mfa_policy = forms.ChoiceField(
        label=gettext_lazy("Autenticação em dois fatores obrigatória para"),
        required=False,
        help_text=gettext_lazy(
            "Quem estiver obrigado e ainda não configurou só vê a página de configuração do MFA ao entrar "
            "nesta organização. A regra da plataforma continua valendo por cima desta."
        ),
    )
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from skeleton.tenancy.models import MFA_POLICY_CHOICES

        self.fields["mfa_policy"].choices = MFA_POLICY_CHOICES


def _platform_mfa_rules() -> list[str]:
    """What the platform already demands, shown so the administrator knows the floor."""
    rules = []
    if getattr(settings, "SKELETON_REQUIRE_PLATFORM_ADMIN_MFA", False):
        rules.append(gettext("O administrador da plataforma sempre configura o MFA."))
    if getattr(settings, "SKELETON_REQUIRE_MFA", False):
        rules.append(gettext("A plataforma exige o MFA de todas as pessoas."))
    elif getattr(settings, "SKELETON_REQUIRE_ADMIN_MFA", False):
        rules.append(gettext("A plataforma exige o MFA de quem administra uma organização."))
    return rules


@tenant_page(admin=True)
@never_cache
@require_http_methods(["GET", "POST"])
def organization_security(request, organization_id):
    from skeleton.accounts.mfa import has_totp
    from skeleton.tenancy.organization_settings import update_mfa_policy

    record = current_settings()
    initial = {"mfa_policy": getattr(record, "mfa_policy", ""), "revision": revision_of(record)}
    form = MFAPolicyForm(request.POST if request.method == "POST" else None, initial=initial)
    status = 200
    if request.method == "POST":
        if set(request.POST) - {"mfa_policy", "revision", "csrfmiddlewaretoken"}:
            form.add_error(None, gettext("Campo não permitido."))
            status = 400
        elif form.is_valid():
            try:
                with transaction.atomic():
                    update_mfa_policy(
                        actor=request.user,
                        policy=form.cleaned_data["mfa_policy"],
                        expected_revision=form.cleaned_data["revision"],
                    )
            except ConcurrentUpdate:
                form.add_error(None, gettext("As configurações mudaram. Atualize a página antes de tentar novamente."))
                status = 409
            except ValidationError as exc:
                _apply_error(form, exc)
                status = 400
            else:
                messages.success(request, gettext("Configurações salvas."))
                return redirect("web:organization-security", organization_id=organization_id)
        else:
            status = 400
    dashboard = reverse("web:dashboard", kwargs={"organization_id": organization_id})
    return render(
        request,
        "skeleton/web/organization_security.html",
        {
            "form": form,
            "organization_id": organization_id,
            "platform_rules": _platform_mfa_rules(),
            "actor_has_totp": has_totp(request.user),
            "cancel_url": dashboard,
            "breadcrumbs": [{"label": gettext("Início"), "url": dashboard}, {"label": gettext("Segurança")}],
        },
        status=status,
    )
