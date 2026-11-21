"""``/platform/appearance/``: the brand and the themes of the instance (#599).

Explicit views over ``skeleton.appearance.services`` and ``skeleton.appearance.images``, not a
generic editor of global records. Only the full platform administrator enters (``platform_page``
without a delegated capability, with MFA); every change also needs CSRF and a recent
authentication, carries the revision it read (an old one answers 409) and accepts only its own
POST fields (anything else is 400). Each write logs a ``security_event`` without values.

These pages load the theme in safe mode (``/brand/theme.css?safe=1``, tokens only): a free CSS
that hides the interface never locks the administrator out. Only the preview of a candidate
theme loads its full CSS, from ``…/preview.css``, never cached publicly.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.logging import security_event
from skeleton.web.views import form_error

from .guards import platform_page

__all__ = [
    "appearance",
    "appearance_activate",
    "appearance_brand",
    "appearance_home",
    "appearance_builtin_preview",
    "appearance_builtin_preview_css",
    "appearance_image",
    "appearance_package_install",
    "appearance_theme",
    "appearance_theme_create",
    "appearance_theme_delete",
    "appearance_theme_preview",
    "appearance_theme_preview_css",
    "appearance_theme_preview_font",
    "appearance_theme_preview_image",
]

CSRF = "csrfmiddlewaretoken"
RADIUS_CHOICES = (
    ("default", gettext_lazy("Padrão")),
    ("sharp", gettext_lazy("Reto")),
    ("round", gettext_lazy("Arredondado")),
)
FONT_CHOICES = (
    ("inter", gettext_lazy("Inter (padrão)")),
    ("system", gettext_lazy("Fonte do sistema")),
    ("serif", gettext_lazy("Serifada do sistema")),
)
IMAGE_KINDS = {
    "logo": gettext_lazy("Logo"),
    "logo_dark": gettext_lazy("Logo para o modo escuro"),
    "icon": gettext_lazy("Ícone"),
}
IMAGE_HELP = {
    "logo": gettext_lazy("PNG, JPEG ou WebP, até 512 KB. Aparece no cabeçalho e nos e-mails sem organização."),
    "logo_dark": gettext_lazy("PNG, JPEG ou WebP, até 512 KB. Usado no modo escuro, junto com o logo."),
    "icon": gettext_lazy("PNG quadrado de 512 × 512 pixels, até 256 KB. Vira o favicon."),
}


class BrandForm(forms.Form):
    product_name = forms.CharField(
        label=gettext_lazy("Nome do produto"),
        required=False,
        max_length=60,
        help_text=gettext_lazy("Aparece no título, no cabeçalho e no rodapé. Vazio mostra “Skeleton”."),
    )
    footer_text = forms.CharField(
        label=gettext_lazy("Texto do rodapé"), required=False, max_length=160, help_text=gettext_lazy("Uma linha.")
    )
    revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)


class ThemeForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=80)
    brand_color = forms.CharField(
        label=gettext_lazy("Cor da marca"),
        required=False,
        max_length=7,
        widget=forms.TextInput(attrs={"placeholder": "#1d4ed8", "spellcheck": "false", "autocomplete": "off"}),
        help_text=gettext_lazy(
            "Formato #RRGGBB, com contraste de pelo menos 4,5:1 com o branco. Vazio mantém a cor do núcleo."
        ),
    )
    radius = forms.ChoiceField(label=gettext_lazy("Cantos"), choices=RADIUS_CHOICES)
    font = forms.ChoiceField(label=gettext_lazy("Fonte"), choices=FONT_CHOICES)
    custom_css = forms.CharField(
        label=gettext_lazy("CSS personalizado"),
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={"rows": 10, "spellcheck": "false", "autocomplete": "off", "class": "font-mono"}),
        help_text=gettext_lazy(
            "Até 32 KB, aplicado depois dos tokens do tema. Não são aceitos @import, url(), image-set(), "
            "expression(), javascript:, behavior, -moz-binding, nem o caractere <. "
            "As páginas de aparência nunca carregam este CSS."
        ),
    )
    revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0, required=False)


def _unknown_fields(request, allowed: set[str], files: set[str] = frozenset()) -> bool:
    return bool(set(request.POST) - allowed - {CSRF} or set(request.FILES) - set(files))


def _log(event: str, request, reason: str) -> None:
    security_event(event, reason_code=reason, source=f"platform:{request.user.pk}")


def package_summary(theme):
    """What a package theme brings, for the remove confirmation and the preview (#637)."""
    if theme is None or theme.source != "package":
        return None
    manifest = theme.package_manifest or {}
    files = theme.package_files or {}
    kinds = {"logo": gettext("logo"), "logo_dark": gettext("logo escuro"), "icon": gettext("ícone")}
    return {
        "id": theme.package_id,
        "version": theme.package_version,
        "publisher": manifest.get("publisher", ""),
        "description": manifest.get("description", ""),
        "compatibility": (manifest.get("compatibility") or {}).get("skeleton", ""),
        "images": ", ".join(str(label) for kind, label in kinds.items() if kind in files) or gettext("nenhuma"),
        "fonts": len(files.get("fonts", []) or []),
        "css_bytes": len((theme.custom_css or "").encode()),
        "product_name": theme.product_name,
        "footer_text": theme.footer_text,
        "digest": theme.package_digest[:12],
    }


def _page(request, *, brand_form=None, image_errors=None, activate_error="", package_error="", status=200):
    from skeleton.appearance.services import current_brand, list_themes, revision_of
    from skeleton.appearance.themes import BUILTIN_THEMES

    brand = current_brand()
    revision = revision_of(brand)
    if brand_form is None:
        brand_form = BrandForm(
            initial={
                "product_name": brand.product_name if brand else "",
                "footer_text": brand.footer_text if brand else "",
                "revision": revision,
            }
        )
    active_builtin = brand.builtin_theme if brand else ""
    active_theme = brand.active_theme_id if brand else None
    images = [
        {
            "kind": kind,
            "label": label,
            "help": IMAGE_HELP[kind],
            "present": bool(brand and getattr(brand, f"{kind}_key")),
            "url": reverse(f"web:brand-{kind.replace('_', '-')}"),
            "version": (getattr(brand, f"{kind}_key") or "")[-12:] if brand else "",
            "errors": (image_errors or {}).get(kind, []),
            "accept": "image/png" if kind == "icon" else "image/png,image/jpeg,image/webp",
        }
        for kind, label in IMAGE_KINDS.items()
    ]
    builtins = [
        {"slug": slug, "name": values.name, "color": values.brand_color, "active": slug == active_builtin}
        for slug, values in BUILTIN_THEMES.items()
    ]
    themes = [{"record": theme, "active": theme.pk == active_theme} for theme in list_themes()]
    from skeleton.appearance.pages import current_home_page

    home = current_home_page()
    return render(
        request,
        "skeleton/web/platform/appearance/index.html",
        {
            "platform_section": "appearance",
            "brand_form": brand_form,
            "revision": revision,
            "images": images,
            "builtins": builtins,
            "themes": themes,
            "core_active": not active_builtin and not active_theme,
            "activate_error": activate_error,
            "package_error": package_error,
            "home_page": {"published": home.published, "blocks": len(home.blocks)},
        },
        status=status,
    )


@platform_page
@require_safe
def appearance(request):
    return _page(request)


@platform_page
@require_POST
@recent_authentication_required
def appearance_brand(request):
    from skeleton.appearance.services import update_brand

    if _unknown_fields(request, {"product_name", "footer_text", "revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    form = BrandForm(request.POST)
    if not form.is_valid():
        return _page(request, brand_form=form, status=400)
    data = form.cleaned_data
    try:
        update_brand(
            actor=request.user,
            data={"product_name": data["product_name"], "footer_text": data["footer_text"]},
            expected_revision=data["revision"],
        )
    except ValidationError as error:
        return _page(request, brand_form=form, status=form_error(form, error))
    _log("appearance.brand_changed", request, "brand")
    messages.success(request, gettext("Marca da instância salva."))
    return redirect("platform:appearance")


@platform_page
@require_POST
@recent_authentication_required
def appearance_image(request, kind):
    from skeleton.appearance.images import remove_image, replace_image

    if kind not in IMAGE_KINDS:
        raise Http404
    if _unknown_fields(request, {"action", "revision"}, {"image"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    action = request.POST.get("action")
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = -1
    try:
        if action == "upload" and "image" in request.FILES:
            replace_image(actor=request.user, kind=kind, upload=request.FILES["image"], expected_revision=revision)
        elif action == "remove":
            remove_image(actor=request.user, kind=kind, expected_revision=revision)
        else:
            raise ValidationError({kind: gettext("Escolha um arquivo de imagem.")})
    except ValidationError as error:
        errors = [message for items in error.message_dict.values() for message in items]
        return _page(request, image_errors={kind: errors}, status=400)
    _log("appearance.image_changed", request, f"{kind}:{action}")
    messages.success(request, gettext("Imagem removida.") if action == "remove" else gettext("Imagem atualizada."))
    return redirect("platform:appearance")


@platform_page
@require_POST
@recent_authentication_required
def appearance_activate(request):
    from skeleton.appearance.services import activate_theme

    if _unknown_fields(request, {"builtin", "theme", "revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = -1
    builtin = request.POST.get("builtin", "")
    theme = request.POST.get("theme") or None
    try:
        activate_theme(actor=request.user, builtin=builtin, theme_id=theme, expected_revision=revision)
    except ValidationError as error:
        detail = " ".join(message for items in error.message_dict.values() for message in items)
        return _page(request, activate_error=detail, status=400)
    _log("appearance.theme_activated", request, "builtin" if builtin else ("theme" if theme else "core"))
    messages.success(request, gettext("Tema ativado. As páginas passam a usá-lo em até 5 segundos."))
    return redirect("platform:appearance")


PACKAGE_MESSAGES = {
    "installed": gettext_lazy("Pacote %(name)s %(version)s instalado. Pré-visualize e ative quando quiser."),
    "updated": gettext_lazy("Pacote %(name)s atualizado de %(previous)s para %(version)s."),
    "unchanged": gettext_lazy("Este pacote já está instalado; nada mudou."),
}


@platform_page
@require_POST
@recent_authentication_required
def appearance_package_install(request):
    """Install, update or recognize a theme package sent as a .zip (#637); never activates it."""
    from skeleton.appearance.package_format import MAX_ARCHIVE_BYTES
    from skeleton.appearance.packages import install_theme_package
    from skeleton.appearance.services import current_brand

    if _unknown_fields(request, set(), {"package"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    upload = request.FILES.get("package")
    if upload is None:
        return _page(request, package_error=gettext("Escolha um arquivo .zip de pacote de tema."), status=400)
    if upload.size > MAX_ARCHIVE_BYTES:
        _log("appearance.package_rejected", request, "too_large")
        return _page(request, package_error=gettext("O pacote pode ter até 4 MiB."), status=400)
    try:
        result = install_theme_package(actor=request.user, data=upload.read())
    except ValidationError as error:
        items = error.error_dict.get("package", []) if hasattr(error, "error_dict") else error.error_list
        code = next((item.code for item in items if item.code), "invalid")
        _log("appearance.package_rejected", request, str(code))
        detail = " ".join(message for item in items for message in item.messages)
        return _page(request, package_error=detail, status=400)
    theme = result.theme
    if result.outcome != "unchanged":
        _log(f"appearance.package_{result.outcome}", request, "package")
    message = str(PACKAGE_MESSAGES[result.outcome]) % {
        "name": theme.name,
        "version": theme.package_version,
        "previous": result.previous_version,
    }
    brand = current_brand()
    if result.outcome == "updated" and brand is not None and brand.active_theme_id == theme.pk:
        message += " " + gettext("É o tema ativo: a mudança já vale para todas as páginas.")
    messages.success(request, message)
    return redirect("platform:appearance")


def _theme_form_page(request, form, *, theme=None, status=200):
    title = gettext("Editar tema") if theme is not None else gettext("Novo tema")
    return render(
        request,
        "skeleton/web/platform/appearance/theme_form.html",
        {
            "platform_section": "appearance",
            "form": form,
            "title": title,
            "theme": theme,
            "cancel_url": reverse("platform:appearance"),
        },
        status=status,
    )


def _theme_data(form) -> dict[str, str]:
    data = form.cleaned_data
    return {name: data[name] for name in ("name", "brand_color", "radius", "font", "custom_css")}


THEME_POST = {"name", "brand_color", "radius", "font", "custom_css", "revision"}


@platform_page
@require_http_methods(["GET", "HEAD", "POST"])
@recent_authentication_required(allow_get=True)
def appearance_theme_create(request):
    from skeleton.appearance.services import create_theme

    if request.method != "POST":
        return _theme_form_page(request, ThemeForm(initial={"radius": "default", "font": "inter"}))
    if _unknown_fields(request, THEME_POST):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    form = ThemeForm(request.POST)
    if not form.is_valid():
        return _theme_form_page(request, form, status=400)
    try:
        create_theme(actor=request.user, data=_theme_data(form))
    except ValidationError as error:
        return _theme_form_page(request, form, status=form_error(form, error))
    _log("appearance.theme_changed", request, "create")
    messages.success(request, gettext("Tema criado. Pré-visualize e ative quando quiser."))
    return redirect("platform:appearance")


def _theme_or_404(sysid):
    from skeleton.appearance.services import get_theme

    theme = get_theme(sysid)
    if theme is None:
        raise Http404
    return theme


@platform_page
@require_http_methods(["GET", "HEAD", "POST"])
@recent_authentication_required(allow_get=True)
def appearance_theme(request, sysid):
    from skeleton.appearance.services import update_theme

    theme = _theme_or_404(sysid)
    if theme.source == "package":
        raise Http404  # A package theme is changed by sending a newer version (#637).
    if request.method != "POST":
        initial = {name: getattr(theme, name) for name in ("name", "brand_color", "radius", "font", "custom_css")}
        return _theme_form_page(request, ThemeForm(initial={**initial, "revision": theme.revision}), theme=theme)
    if _unknown_fields(request, THEME_POST):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    form = ThemeForm(request.POST)
    if not form.is_valid():
        return _theme_form_page(request, form, theme=theme, status=400)
    revision = form.cleaned_data.get("revision")
    try:
        update_theme(
            actor=request.user,
            theme_id=theme.pk,
            data=_theme_data(form),
            expected_revision=revision if revision is not None else -1,
        )
    except ValidationError as error:
        return _theme_form_page(request, form, theme=theme, status=form_error(form, error))
    _log("appearance.theme_changed", request, "update")
    messages.success(request, gettext("Tema salvo."))
    return redirect("platform:appearance")


@platform_page
@require_http_methods(["GET", "HEAD", "POST"])
@recent_authentication_required(allow_get=True)
def appearance_theme_delete(request, sysid):
    from skeleton.appearance.services import delete_theme

    theme = _theme_or_404(sysid)
    package = package_summary(theme)
    if package:
        message = gettext(
            "O tema sai do catálogo (%(id)s %(version)s). A remoção é lógica e fica na auditoria; os arquivos "
            "do pacote são apagados depois da carência. Para voltar, envie o pacote de novo."
        ) % {"id": package["id"], "version": package["version"]}
    else:
        message = gettext("O tema sai do catálogo. A exclusão é lógica e fica na auditoria.")
    context = {
        "platform_section": "appearance",
        "eyebrow": gettext("Aparência"),
        "title": gettext("Remover pacote de tema") if package else gettext("Excluir tema"),
        "subject_label": theme.name,
        "message": message,
        "submit_label": gettext("Remover pacote") if package else gettext("Excluir tema"),
        "cancel_url": reverse("platform:appearance"),
        "form": _RevisionForm(initial={"revision": theme.revision}),
    }
    if request.method != "POST":
        return render(request, "skeleton/web/platform/confirm.html", context)
    if _unknown_fields(request, {"revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = -1
    try:
        delete_theme(actor=request.user, theme_id=theme.pk, expected_revision=revision)
    except ValidationError as error:
        context["error"] = " ".join(message for items in error.message_dict.values() for message in items)
        return render(request, "skeleton/web/platform/confirm.html", context, status=400)
    if package:
        _log("appearance.package_removed", request, "package")
        messages.success(request, gettext("Pacote de tema removido."))
        return redirect("platform:appearance")
    _log("appearance.theme_changed", request, "delete")
    messages.success(request, gettext("Tema excluído."))
    return redirect("platform:appearance")


class _RevisionForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput)


def _preview_page(request, *, name, css_url, theme=None):
    context = {"platform_section": "appearance", "theme_name": name, "preview_css_url": css_url}
    context["package"] = package_summary(theme)
    if theme is not None:
        from skeleton.appearance.assets import package_assets

        kinds = {ref.name for ref in package_assets(theme)}
        for kind in ("logo", "logo_dark"):
            if kind in kinds and "logo" in kinds:
                url = reverse("platform:appearance-theme-preview-image", kwargs={"sysid": theme.pk, "kind": kind})
                context[f"preview_{kind}_url"] = f"{url}?r={theme.revision}"
    return render(request, "skeleton/web/platform/appearance/preview.html", context)


def _builtin_or_404(slug):
    from skeleton.appearance.themes import BUILTIN_THEMES

    if slug not in BUILTIN_THEMES:
        raise Http404
    return BUILTIN_THEMES[slug]


def _theme_values(theme):
    """The candidate theme with its fonts served by the preview route (never cached publicly)."""
    from skeleton.appearance.assets import package_theme_values

    def font_url(ref):
        url = reverse("platform:appearance-theme-preview-font", kwargs={"sysid": theme.pk, "name": ref.name})
        return f"{url}?v={ref.version}"

    return package_theme_values(theme, font_url=font_url)


@platform_page
@require_safe
def appearance_theme_preview(request, sysid):
    theme = _theme_or_404(sysid)
    url = reverse("platform:appearance-theme-preview-css", kwargs={"sysid": theme.pk}) + f"?r={theme.revision}"
    return _preview_page(request, name=theme.name, css_url=url, theme=theme)


@platform_page
@require_safe
def appearance_builtin_preview(request, slug):
    values = _builtin_or_404(slug)
    return _preview_page(
        request, name=values.name, css_url=reverse("platform:appearance-builtin-preview-css", args=[slug])
    )


def _preview_css(request, values):
    from skeleton.appearance.themes import theme_css, theme_digest
    from skeleton.web.brand_views import stylesheet_response

    css = theme_css(values)
    return stylesheet_response(request, css, theme_digest(css), cache_control="private, no-store")


@platform_page
@require_safe
def appearance_theme_preview_css(request, sysid):
    return _preview_css(request, _theme_values(_theme_or_404(sysid)))


@platform_page
@require_safe
def appearance_builtin_preview_css(request, slug):
    return _preview_css(request, _builtin_or_404(slug))


def _preview_asset(request, sysid, name):
    from skeleton.appearance.assets import open_asset, package_assets
    from skeleton.web.brand_views import asset_response

    theme = _theme_or_404(sysid)
    ref = next((item for item in package_assets(theme) if item.name == name), None)
    if ref is None:
        raise Http404
    return asset_response(request, open_asset(ref), ref.mime, ref.version, cache_control="private, no-store")


@platform_page
@require_safe
def appearance_theme_preview_font(request, sysid, name):
    """A font of a candidate package theme, only for the administrator (#636)."""
    from skeleton.web.brand_views import FONT_NAME

    if not FONT_NAME.fullmatch(name):
        raise Http404
    return _preview_asset(request, sysid, name)


@platform_page
@require_safe
def appearance_theme_preview_image(request, sysid, kind):
    """The logo or the dark logo of a candidate package theme, only for the administrator (#636)."""
    if kind not in ("logo", "logo_dark"):
        raise Http404
    return _preview_asset(request, sysid, kind)


# --------------------------------------------------------------------------- public home page (#622)


class _HomePageStore:
    """The editor's store over ``skeleton.appearance.pages``: always the current row, never the cache."""

    def __init__(self, actor):
        self.actor = actor

    def load(self):
        from skeleton.appearance.pages import current_home_page

        return current_home_page()

    def save(self, *, document, published, expected_revision):
        from skeleton.appearance.pages import save_home_page

        return save_home_page(
            actor=self.actor, document=document, published=published, expected_revision=expected_revision
        )


def _home_preview(request, state):
    return render(
        request,
        "skeleton/web/pages/public.html",
        {
            "blocks": state.blocks,
            "preview": True,
            "published": state.published,
            "back_url": reverse("platform:appearance-home"),
        },
    )


def home_page_editor(request):
    from skeleton.common.page_blocks import PLATFORM_HOME
    from skeleton.web.page_editor import PageEditor

    return PageEditor(
        kind=PLATFORM_HOME,
        store=_HomePageStore(request.user),
        url_name="platform:appearance-home",
        base_template="skeleton/web/platform/base.html",
        title=gettext("Página inicial pública"),
        eyebrow=gettext("Aparência"),
        description=gettext(
            "O que quem não entrou vê na raiz da plataforma. Sem página publicada, a raiz leva ao login, "
            "como antes. Os blocos são texto simples; a marca e o tema da instância valem na página."
        ),
        preview=_home_preview,
        extra_context={"platform_section": "appearance"},
    )


@platform_page
@recent_authentication_required(allow_get=True)
def appearance_home(request, operation, index=None):
    """``/platform/appearance/home/``: the shared block editor (#619) over the instance home page."""
    from skeleton.web.page_editor import dispatch

    # Each write logs appearance.home_page_changed (without the content) in save_home_page.
    return dispatch(request, home_page_editor(request), operation, index=index)
