"""Bundles of apps (#714) at /platform/bundles/ and the bundle of one organization."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system import bundles
from skeleton.web.views import form_error

from .guards import platform_page


class BundleForm(forms.Form):
    code = forms.SlugField(label=gettext_lazy("Código"), max_length=64, help_text=gettext_lazy("Não muda depois."))
    label = forms.CharField(label=gettext_lazy("Nome"), max_length=200)
    description = forms.CharField(
        label=gettext_lazy("Descrição"), required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 3})
    )
    apps = forms.MultipleChoiceField(
        label=gettext_lazy("Apps"), choices=(), required=False, widget=forms.CheckboxSelectMultiple
    )
    position = forms.IntegerField(label=gettext_lazy("Ordem"), min_value=0, initial=0)
    is_default = forms.BooleanField(label=gettext_lazy("Padrão"), required=False)
    signup_visible = forms.BooleanField(label=gettext_lazy("Visível no cadastro"), required=False)
    is_active = forms.BooleanField(label=gettext_lazy("Ativo"), required=False, initial=True)
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        from skeleton.system.app_registry import all_apps

        super().__init__(*args, **kwargs)
        # Libraries come with the apps that use them (#792): never a choice of their own.
        self.fields["apps"].choices = [
            (manifest.app_label, manifest.label)
            for manifest in sorted(all_apps(), key=lambda item: item.label)
            if not manifest.library
        ]


def _initial(bundle):
    if bundle is None:
        return {"expected_revision": 0, "is_active": True, "position": 0}
    return {**{name: getattr(bundle, name) for name in bundles.FIELDS}, "expected_revision": bundle.revision}


def _page(request, bundle, form, *, status=200):
    from skeleton.system.models import AppBundle

    return render(
        request,
        "skeleton/web/platform/bundles.html",
        {
            "platform_section": "bundles",
            "bundles": AppBundle.objects.order_by("position", "label"),
            "bundle": bundle,
            "form": form,
        },
        status=status,
    )


def _save(request, bundle):
    form = BundleForm(request.POST)
    if not form.is_valid():
        return _page(request, bundle, form, status=400)
    data = dict(form.cleaned_data)
    revision = data.pop("expected_revision")
    data["apps"] = list(data["apps"])
    try:
        with transaction.atomic():
            saved = bundles.save_bundle(
                actor=request.user, data=data, sysid=bundle.pk if bundle else None, expected_revision=revision
            )
    except (ValidationError, ConcurrentUpdate) as exc:
        status = form_error(form, exc)
        return _page(request, bundle, form, status=status)
    messages.success(request, gettext("Pacote salvo."))
    return redirect("platform:bundle", sysid=saved.pk)


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def bundle_list(request):
    if request.method == "POST":
        return _save(request, None)
    return _page(request, None, BundleForm(initial=_initial(None)))


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def bundle_detail(request, sysid):
    from skeleton.system.models import AppBundle

    bundle = get_object_or_404(AppBundle.objects, pk=sysid)
    if request.method == "POST":
        return _save(request, bundle)
    return _page(request, bundle, BundleForm(initial=_initial(bundle)))


def _limit_text(value):
    return gettext("ilimitado") if value is None else str(value)


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def organization_bundle(request, sysid):
    """GET ``?bundle=<code>`` previews the change; POST applies it (``lock_apps`` optional)."""
    from skeleton.system.models import AppBundle
    from skeleton.tenancy.models import Organization

    organization = get_object_or_404(Organization.objects, pk=sysid, is_active=True)
    code = request.POST.get("bundle") if request.method == "POST" else request.GET.get("bundle")
    bundle = AppBundle.objects.filter(code=code or "", is_active=True).first()
    if bundle is None:
        raise Http404
    if request.method != "POST":
        preview = bundles.bundle_change_preview(actor=request.user, organization_id=organization.pk, bundle=bundle)
        return render(
            request,
            "skeleton/web/platform/organization_bundle.html",
            {"platform_section": "organizations", "subject": organization, "bundle": bundle, "preview": preview},
        )
    before = bundles.bundle_change_preview(actor=request.user, organization_id=organization.pk, bundle=bundle)
    try:
        with transaction.atomic():
            bundles.assign_bundle(
                actor=request.user,
                organization_id=organization.pk,
                bundle=bundle,
                lock_apps=request.POST.get("lock_apps") == "on",
            )
    except (ValidationError, ConcurrentUpdate) as exc:
        detail = " ".join(exc.messages) if isinstance(exc, ValidationError) else gettext("A organização mudou.")
        messages.error(request, detail)
        return redirect("platform:organization", sysid=organization.pk)
    messages.success(
        request,
        gettext("Pacote %(bundle)s atribuído. Limite de membros: %(before)s → %(after)s")
        % {
            "bundle": bundle.label,
            "before": _limit_text(before.members_before),
            "after": _limit_text(before.members_after),
        },
    )
    return redirect("platform:organization", sysid=organization.pk)
