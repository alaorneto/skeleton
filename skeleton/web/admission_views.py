"""Join requests (#26): ``/join/`` for the person, ``/o/<org>/admission/`` and ``/requests/`` for deciders.

``/join/<org>/`` answers the same 404 as a missing organization whenever the organization
does not accept requests. Every decision and withdrawal carries the revision the page
showed; a stale form answers 409.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from skeleton.access.capabilities import ADMISSION_CONFIGURE, ADMISSION_DECIDE
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy import admission
from skeleton.tenancy.context import current_context

from .views import tenant_page

STATUS_LABELS = {
    "awaiting_verification": "Aguardando confirmação do email",
    "pending": "Aguardando aprovação",
    "approved": "Aprovado",
    "rejected": "Não aprovado",
    "withdrawn": "Retirado",
    "expired": "Expirado",
}


def _global_page(view):
    """Pages of the person outside any organization: login and no tenant in the URL."""

    @login_required
    def wrapped(request, *args, **kwargs):
        if current_context().organization_id is not None:
            raise Http404
        return view(request, *args, **kwargs)

    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return wrapped


def request_rows(user):
    rows = []
    for item in admission.my_join_requests(user):
        status = admission.effective_status(item)
        rows.append(
            {
                "sysid": item.pk,
                "organization_name": item.organization.name,
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "revision": item.revision,
                "open": status in ("pending", "awaiting_verification"),
                "created_at": item.created_at,
            }
        )
    return rows


class JoinRequestForm(forms.Form):
    message = forms.CharField(
        label="Mensagem para quem administra (opcional)",
        max_length=admission.MESSAGE_MAX,
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Diga quem você é e por que quer entrar. Quem decide vê seu nome e seu email.",
    )


def _rate_limited(request, retry_after):
    response = render(
        request,
        "skeleton/web/error.html",
        {"detail": "Muitas solicitações em pouco tempo. Tente de novo mais tarde."},
        status=429,
    )
    response["Retry-After"] = str(retry_after)
    return response


@_global_page
@require_safe
def join_directory(request):
    verified = admission.has_verified_email(request.user)
    return render(
        request,
        "skeleton/web/join/directory.html",
        {
            "verified": verified,
            "organizations": admission.directory(request.user),
            "requests": request_rows(request.user),
        },
    )


@_global_page
@require_http_methods(["GET", "POST"])
def join_organization(request, organization_id):
    if not admission.has_verified_email(request.user):
        return render(request, "skeleton/web/join/unverified.html", status=403)
    form = JoinRequestForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        if not form.is_valid():
            return _join_page(request, organization_id, form, status=400)
        try:
            with transaction.atomic():
                admission.submit_join_request(
                    user=request.user, organization_id=organization_id, message=form.cleaned_data["message"]
                )
        except admission.JoinRateLimited as limited:
            return _rate_limited(request, limited.retry_after)
        except admission.JoinUnavailable:
            raise Http404 from None
        except admission.AlreadyMember:
            return redirect("web:dashboard", organization_id=organization_id)
        messages.success(request, "Pedido enviado. Você recebe um email quando ele for decidido.")
        return redirect("web:join")
    return _join_page(request, organization_id, form)


def _join_page(request, organization_id, form, *, status=200):
    try:
        target = admission.accepting(organization_id)
    except admission.JoinUnavailable:
        raise Http404 from None
    return render(request, "skeleton/web/join/request.html", {"form": form, "target": target}, status=status)


@_global_page
@require_POST
def join_withdraw(request, sysid):
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = None
    try:
        with transaction.atomic():
            admission.withdraw_join_request(user=request.user, join_request_id=sysid, expected_revision=revision)
    except admission.JoinUnavailable:
        raise Http404 from None
    except (ConcurrentUpdate, ValidationError):
        return render(
            request,
            "skeleton/web/error.html",
            {"detail": "O pedido mudou desde que a página foi aberta. Atualize e tente de novo."},
            status=409,
        )
    messages.success(request, "Pedido retirado.")
    return redirect("web:join")


# --------------------------------------------------------------------------- organization


class AdmissionPolicyForm(forms.Form):
    join_requests_enabled = forms.BooleanField(
        label="Aceitar pedidos de ingresso",
        required=False,
        help_text="Pessoas com email verificado podem pedir para entrar; alguém com permissão aprova ou recusa.",
    )
    discoverable = forms.BooleanField(
        label="Aparecer na lista de organizações",
        required=False,
        help_text="Lista a organização em /join/ para quem tem email verificado. Sem isso, o pedido só chega "
        "por quem tiver o endereço da organização.",
    )
    request_default_role = forms.ModelChoiceField(
        label="Papel de quem for aprovado",
        queryset=None,
        required=False,
        empty_label="Membro (sem permissões)",
        help_text="Um papel com capacidades administrativas só pode ser aprovado pelo administrador.",
    )
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        from skeleton.access.models import Role

        super().__init__(*args, **kwargs)
        field = self.fields["request_default_role"]
        field.queryset = Role.objects.exclude(code="organization_admin", is_builtin=True).order_by("name")
        field.label_from_instance = lambda role: role.name


@tenant_page(capability=ADMISSION_CONFIGURE)
@require_http_methods(["GET", "POST"])
def admission_policy(request, organization_id):
    record = admission.current_policy()
    initial = {
        "join_requests_enabled": bool(record and record.join_requests_enabled),
        "discoverable": bool(record and record.discoverable),
        "request_default_role": record.request_default_role_id if record else None,
        "revision": admission.revision_of(record),
    }
    form = AdmissionPolicyForm(request.POST if request.method == "POST" else None, initial=initial)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = dict(form.cleaned_data)
            revision = data.pop("revision")
            try:
                with transaction.atomic():
                    admission.configure_policy(actor=request.user, data=data, expected_revision=revision)
            except ConcurrentUpdate:
                form.add_error(None, "A política mudou desde que a página foi aberta. Atualize e tente de novo.")
                status = 409
            except ValidationError as exc:
                for field, problems in exc.message_dict.items():
                    form.add_error(field if field in form.fields else None, problems)
                status = 400
            else:
                messages.success(request, "Política de admissão salva.")
                return redirect("web:admission", organization_id=organization_id)
        else:
            status = 400
    dashboard = reverse("web:dashboard", kwargs={"organization_id": organization_id})
    return render(
        request,
        "skeleton/web/admission/policy.html",
        {
            "form": form,
            "organization_id": organization_id,
            "join_url": request.build_absolute_uri(reverse("web:join-organization", args=[organization_id])),
            "breadcrumbs": [{"label": "Início", "url": dashboard}, {"label": "Admissão"}],
        },
        status=status,
    )


@tenant_page(capability=ADMISSION_DECIDE)
@require_safe
def join_requests(request, organization_id):
    return _queue_page(request, organization_id)


def _queue_page(request, organization_id, *, status=200, error=None):
    rows = list(admission.pending_requests()[:200])
    returning = admission.previous_members([row.user_id for row in rows])
    dashboard = reverse("web:dashboard", kwargs={"organization_id": organization_id})
    return render(
        request,
        "skeleton/web/admission/requests.html",
        {
            "organization_id": organization_id,
            "rows": [{"record": row, "returning": row.user_id in returning} for row in rows],
            "error": error,
            "breadcrumbs": [{"label": "Início", "url": dashboard}, {"label": "Solicitações de ingresso"}],
        },
        status=status,
    )


@tenant_page(capability=ADMISSION_DECIDE)
@require_POST
def join_request_decide(request, organization_id, sysid):
    decision = request.POST.get("decision")
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = None
    if decision not in ("approve", "reject"):
        return _queue_page(request, organization_id, status=400, error="Escolha aprovar ou recusar.")
    try:
        with transaction.atomic():
            record = admission.decide_join_request(
                actor=request.user,
                join_request_id=sysid,
                approve=decision == "approve",
                expected_revision=revision,
                request=request,
            )
    except admission.JoinUnavailable:
        raise Http404 from None
    except PermissionDenied:
        return _queue_page(
            request,
            organization_id,
            status=403,
            error="Só o administrador aprova um pedido com papel que tem capacidades administrativas.",
        )
    except (ConcurrentUpdate, ValidationError):
        return _queue_page(
            request,
            organization_id,
            status=409,
            error="O pedido mudou ou já foi decidido. Confira a lista atualizada.",
        )
    if record.status == "approved":
        messages.success(request, f"{record.user.email} agora é membro da organização.")
    else:
        messages.success(request, f"Pedido de {record.user.email} recusado.")
    return redirect("web:join-requests", organization_id=organization_id)
