"""Administration of the organization's external people (#532): portal policy, accesses and invitations."""

from django import forms
from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from skeleton.access.capabilities import EXTERNAL_MANAGE
from skeleton.service_api import Conflict, Forbidden, NotFound
from skeleton.service_api.external import ExternalAccessService
from skeleton.web.views import tenant_page


class PortalPolicyForm(forms.Form):
    enabled = forms.BooleanField(label=gettext_lazy("Portal ligado"), required=False)
    invitation_ttl_hours = forms.IntegerField(
        label=gettext_lazy("Validade dos convites (horas)"), min_value=1, max_value=720
    )
    revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)


def _revision(request):
    try:
        return int(request.POST.get("revision", ""))
    except ValueError:
        raise Http404 from None


@tenant_page(capability=EXTERNAL_MANAGE)
@require_http_methods(["GET", "POST"])
def external_admin(request, organization_id):
    from skeleton.common.exceptions import ConcurrentUpdate
    from skeleton.tenancy import external

    current = external.policy()
    initial = {
        "enabled": current.enabled,
        "invitation_ttl_hours": current.invitation_ttl_hours,
        "revision": current.revision if current.pk and not current._state.adding else 0,
    }
    form = PortalPolicyForm(request.POST or None, initial=initial)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            try:
                with transaction.atomic():
                    external.configure_policy(
                        user=request.user,
                        enabled=data["enabled"],
                        invitation_ttl_hours=data["invitation_ttl_hours"],
                        expected_revision=data["revision"],
                    )
            except ConcurrentUpdate:
                form.add_error(
                    None, gettext("A política mudou desde que a página foi aberta. Atualize e tente de novo.")
                )
                status = 409
            else:
                messages.success(request, gettext("Política do portal salva."))
                return redirect("web:external", organization_id=organization_id)
        else:
            status = 400
    service = ExternalAccessService()
    return render(
        request,
        "skeleton/web/external/admin.html",
        {
            "organization_id": organization_id,
            "form": form,
            "accesses": service.accesses(actor=request.user, page_size=100).items,
            "invitations": service.invitations(actor=request.user),
        },
        status=status,
    )


@tenant_page(capability=EXTERNAL_MANAGE)
@require_POST
@transaction.atomic
def external_access_revoke(request, organization_id, sysid):
    try:
        ExternalAccessService().revoke(actor=request.user, access_id=sysid, expected_revision=_revision(request))
    except (NotFound, Forbidden):
        raise Http404 from None
    except Conflict:
        messages.error(request, gettext("O acesso mudou. Atualize a página e tente de novo."))
    else:
        messages.success(request, gettext("Acesso revogado."))
    return redirect("web:external", organization_id=organization_id)


@tenant_page(capability=EXTERNAL_MANAGE)
@require_POST
@transaction.atomic
def external_invitation_revoke(request, organization_id, sysid):
    try:
        ExternalAccessService().revoke_invitation(
            actor=request.user, invitation_id=sysid, expected_revision=_revision(request)
        )
    except (NotFound, Forbidden):
        raise Http404 from None
    except Conflict:
        messages.error(request, gettext("O convite mudou. Atualize a página e tente de novo."))
    else:
        messages.success(request, gettext("Convite revogado."))
    return redirect("web:external", organization_id=organization_id)
