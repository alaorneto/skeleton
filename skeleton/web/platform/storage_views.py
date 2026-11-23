"""The platform's storage locations (#704) and the location of one organization (#705).

Only the platform administrator, with recent authentication. The secret key is a password
field that is never rendered back; the detail says only whether it is set.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.files.locations import deactivate_location, save_location, test_location
from skeleton.web.views import form_error

from .guards import platform_page


class StorageLocationForm(forms.Form):
    code = forms.SlugField(label=gettext_lazy("Código"), max_length=64, help_text=gettext_lazy("Não muda depois."))
    label = forms.CharField(label=gettext_lazy("Nome"), max_length=200)
    backend = forms.ChoiceField(
        label=gettext_lazy("Tipo"),
        choices=(("s3", "S3"), ("filesystem", gettext_lazy("Diretório local (desenvolvimento)"))),
        help_text=gettext_lazy("Não muda depois."),
    )
    bucket_name = forms.CharField(label=gettext_lazy("Bucket"), max_length=63, required=False)
    region_name = forms.CharField(label=gettext_lazy("Região"), max_length=64, required=False)
    endpoint_url = forms.CharField(
        label=gettext_lazy("Endpoint"),
        max_length=300,
        required=False,
        help_text=gettext_lazy("https://… de um S3 compatível; vazio para a AWS."),
    )
    addressing_style = forms.ChoiceField(
        label=gettext_lazy("Endereçamento"),
        choices=(("auto", "auto"), ("path", "path"), ("virtual", "virtual")),
        required=False,
    )
    key_prefix = forms.CharField(label=gettext_lazy("Prefixo das chaves"), max_length=100, required=False)
    access_key_id = forms.CharField(label=gettext_lazy("Access key ID"), max_length=128, required=False)
    secret_access_key = forms.CharField(
        label=gettext_lazy("Secret access key"),
        required=False,
        strip=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy("Vazio mantém a chave gravada."),
    )
    directory = forms.CharField(
        label=gettext_lazy("Diretório"),
        max_length=300,
        required=False,
        help_text=gettext_lazy("Só no tipo diretório: subdiretório de SKELETON_FILES_LOCATIONS_ROOT."),
    )
    is_default = forms.BooleanField(label=gettext_lazy("Padrão das organizações novas"), required=False)
    expected_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def data_for_service(self):
        data = dict(self.cleaned_data)
        data.pop("expected_revision")
        data["addressing_style"] = data.get("addressing_style") or "auto"
        return data


def _initial(location):
    if location is None:
        return {"expected_revision": 0, "addressing_style": "auto"}
    names = (
        "code",
        "label",
        "backend",
        "bucket_name",
        "region_name",
        "endpoint_url",
        "addressing_style",
        "key_prefix",
        "access_key_id",
        "directory",
        "is_default",
    )
    return {**{name: getattr(location, name) for name in names}, "expected_revision": location.revision}


def _location_page(request, location, form, *, status=200, result=None):
    return render(
        request,
        "skeleton/web/platform/storage_location.html",
        {
            "platform_section": "storage",
            "location": location,
            "form": form,
            "secret_set": bool(location is not None and getattr(location.secret_access_key, "token", "")),
            "result": result,
        },
        status=status,
    )


@platform_page
@require_http_methods(["GET"])
@recent_authentication_required(allow_get=True)
def storage_locations(request):
    from skeleton.files.models import OrganizationStorage, StorageLocation

    counts = {}
    for location_id in OrganizationStorage.objects.values_list("location_id", flat=True):
        counts[location_id] = counts.get(location_id, 0) + 1
    rows = [
        {"location": row, "organizations": counts.get(row.pk, 0)} for row in StorageLocation.objects.order_by("label")
    ]
    return render(
        request, "skeleton/web/platform/storage_locations.html", {"platform_section": "storage", "rows": rows}
    )


def _save(request, location):
    form = StorageLocationForm(request.POST)
    if not form.is_valid():
        return _location_page(request, location, form, status=400)
    try:
        with transaction.atomic():
            saved = save_location(
                actor=request.user,
                data=form.data_for_service(),
                sysid=location.pk if location is not None else None,
                expected_revision=form.cleaned_data["expected_revision"],
            )
    except (ValidationError, ConcurrentUpdate) as exc:
        status = form_error(form, exc)
        return _location_page(request, location, form, status=status)
    messages.success(request, gettext("Local de armazenamento salvo."))
    return redirect("platform:storage-location", sysid=saved.pk)


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def storage_location_create(request):
    if request.method == "POST":
        return _save(request, None)
    return _location_page(request, None, StorageLocationForm(initial=_initial(None)))


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def storage_location(request, sysid):
    from skeleton.files.models import StorageLocation

    location = get_object_or_404(StorageLocation.objects, pk=sysid)
    if request.method == "POST":
        if request.POST.get("action") == "deactivate":
            try:
                with transaction.atomic():
                    deactivate_location(
                        actor=request.user, sysid=location.pk, expected_revision=int(request.POST.get("revision", -1))
                    )
            except (ValidationError, ConcurrentUpdate, ValueError) as exc:
                form = StorageLocationForm(initial=_initial(location))
                detail = " ".join(exc.messages) if isinstance(exc, ValidationError) else gettext("O local mudou.")
                form.add_error(None, detail)
                return _location_page(request, location, form, status=409)
            messages.success(request, gettext("Local desativado."))
            return redirect("platform:storage")
        return _save(request, location)
    return _location_page(request, location, StorageLocationForm(initial=_initial(location)))


@platform_page
@require_POST
@recent_authentication_required
def storage_location_test(request, sysid):
    from skeleton.files.models import StorageLocation

    location = get_object_or_404(StorageLocation.objects, pk=sysid)
    result = test_location(actor=request.user, sysid=location.pk)
    return _location_page(request, location, StorageLocationForm(initial=_initial(location)), result=result)


@platform_page
@require_POST
@recent_authentication_required
@transaction.atomic
def organization_storage_location(request, sysid):
    """Point one organization at a location; refused while it has files (``storage_in_use``)."""
    from skeleton.files.locations import assign_storage
    from skeleton.files.models import StorageLocation
    from skeleton.tenancy.models import Organization

    organization = get_object_or_404(Organization.objects, pk=sysid)
    chosen = request.POST.get("location") or None
    location = None
    if chosen is not None:
        location = StorageLocation.objects.filter(pk=chosen, is_active=True).first()
        if location is None:
            messages.error(request, gettext("Escolha um local ativo."))
            return redirect("platform:organization", sysid=organization.pk)
    try:
        with transaction.atomic():
            assign_storage(actor=request.user, organization=organization, location=location)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("platform:organization", sysid=organization.pk)
    messages.success(request, gettext("Local de armazenamento da organização atualizado."))
    return redirect("platform:organization", sysid=organization.pk)
