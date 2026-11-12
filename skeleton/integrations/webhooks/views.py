"""HTML pages of the organization's webhooks: ``/o/<org>/integrations/webhooks/`` (#63).

Only ``integrations.manage`` sees them (404 otherwise, as if they did not exist). Every POST
needs CSRF and a recent authentication; the signing secret is rendered once, by creation and
rotation, on a page that is never cached.
"""

from functools import wraps

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context

from . import services

TEMPLATES = "skeleton/integrations/webhooks/"


def integrations_page(view):
    """Login, tenant, ``integrations.manage`` (404 without it) and MFA for capability holders."""

    @login_required
    @never_cache
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        from skeleton.access.capabilities import INTEGRATIONS_MANAGE, has_capability
        from skeleton.accounts.mfa import mfa_required

        if current_context().organization_id is None or not has_capability(request.user, INTEGRATIONS_MANAGE):
            raise Http404
        if mfa_required(request.user):
            return redirect("mfa_index")
        return view(request, *args, **kwargs)

    return wrapper


class EndpointForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=150)
    url = forms.CharField(
        label="URL HTTPS", max_length=2000, help_text=gettext_lazy("Validada contra destinos internos ao salvar.")
    )
    principal = forms.ChoiceField(
        label=gettext_lazy("Conta de serviço"),
        help_text=gettext_lazy("As entregas só falam de registros que essa conta pode ler."),
    )
    event_types = forms.MultipleChoiceField(label=gettext_lazy("Eventos"), widget=forms.CheckboxSelectMultiple)
    is_enabled = forms.BooleanField(label=gettext_lazy("Ativo"), required=False, initial=True)
    revision = forms.IntegerField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from skeleton.accounts.models import AccountType, User

        accounts = User.objects.filter(
            account_type=AccountType.SERVICE,
            service_organization_id=current_context().organization_id,
            is_active=True,
        ).order_by("first_name", "sysid")
        self.fields["principal"].choices = [(str(user.pk), user.first_name or user.email) for user in accounts]
        self.fields["event_types"].choices = [(code, code) for code in services.available_event_types()]

    def service_data(self):
        data = dict(self.cleaned_data)
        data.pop("revision", None)
        return data


def _apply_error(form, error):
    if isinstance(error, ConcurrentUpdate):
        form.add_error(None, gettext("O webhook mudou. Atualize a página antes de tentar novamente."))
        return 409
    for field, problems in error.message_dict.items():
        form.add_error(field if field in form.fields else None, problems)
    return 400


def _endpoint(sysid):
    from .models import WebhookEndpoint

    endpoint = WebhookEndpoint.objects.filter(pk=sysid).first()
    if endpoint is None:
        raise Http404
    return endpoint


def _secret_page(request, organization_id, issued, *, created):
    return render(
        request,
        TEMPLATES + "secret.html",
        {"organization_id": organization_id, "endpoint": issued.endpoint, "secret": issued.secret, "created": created},
        status=201 if created else 200,
    )


@integrations_page
def endpoint_list(request, organization_id):
    from .models import WebhookEndpoint

    endpoints = WebhookEndpoint.objects.select_related("principal").order_by("name", "sysid")
    return render(request, TEMPLATES + "list.html", {"organization_id": organization_id, "endpoints": endpoints})


@integrations_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def endpoint_create(request, organization_id):
    form = EndpointForm(request.POST or None)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    issued = services.create_endpoint(actor=request.user, data=form.service_data())
            except ValidationError as exc:
                status = _apply_error(form, exc)
            else:
                return _secret_page(request, organization_id, issued, created=True)
        else:
            status = 400
    return render(
        request,
        TEMPLATES + "form.html",
        {"organization_id": organization_id, "form": form, "endpoint": None},
        status=status,
    )


@integrations_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def endpoint_detail(request, organization_id, sysid):
    endpoint = _endpoint(sysid)
    initial = {
        "name": endpoint.name,
        "url": endpoint.url,
        "principal": str(endpoint.principal_id),
        "event_types": endpoint.event_types,
        "is_enabled": endpoint.is_enabled,
        "revision": endpoint.revision,
    }
    form = EndpointForm(request.POST or None, initial=initial)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    services.update_endpoint(
                        endpoint,
                        actor=request.user,
                        data=form.service_data(),
                        expected_revision=form.cleaned_data["revision"] or 0,
                    )
            except (ValidationError, ConcurrentUpdate) as exc:
                status = _apply_error(form, exc)
            else:
                messages.success(request, gettext("Webhook atualizado."))
                return redirect("integrations:webhook", organization_id=organization_id, sysid=endpoint.pk)
        else:
            status = 400
    return render(
        request,
        TEMPLATES + "form.html",
        {"organization_id": organization_id, "form": form, "endpoint": endpoint},
        status=status,
    )


def _revision(request):
    try:
        return int(request.POST.get("revision", ""))
    except ValueError:
        return -1


@integrations_page
@require_POST
@recent_authentication_required
@transaction.atomic
def endpoint_rotate(request, organization_id, sysid):
    endpoint = _endpoint(sysid)
    try:
        with transaction.atomic():
            issued = services.rotate_secret(endpoint, actor=request.user, expected_revision=_revision(request))
    except ConcurrentUpdate:
        messages.error(request, gettext("O webhook mudou. Atualize a página antes de tentar novamente."))
        return redirect("integrations:webhook", organization_id=organization_id, sysid=endpoint.pk)
    return _secret_page(request, organization_id, issued, created=False)


@integrations_page
@require_POST
@recent_authentication_required
@transaction.atomic
def endpoint_delete(request, organization_id, sysid):
    endpoint = _endpoint(sysid)
    try:
        with transaction.atomic():
            services.delete_endpoint(endpoint, actor=request.user, expected_revision=_revision(request))
    except ConcurrentUpdate:
        messages.error(request, gettext("O webhook mudou. Atualize a página antes de tentar novamente."))
        return redirect("integrations:webhook", organization_id=organization_id, sysid=endpoint.pk)
    messages.success(request, gettext("Webhook %(name)s removido.") % {"name": endpoint.name})
    return redirect(reverse("integrations:webhooks", kwargs={"organization_id": organization_id}))


@integrations_page
def delivery_list(request, organization_id, sysid):
    """The endpoint's deliveries, newest first; the receiver's answer is shown escaped and at most 1 KB."""
    from skeleton.web.views import pagination

    from .models import WebhookDelivery

    endpoint = _endpoint(sysid)
    rows = pagination(request, WebhookDelivery.objects.filter(endpoint=endpoint).order_by("-created_at", "sysid"))
    return render(
        request,
        TEMPLATES + "deliveries.html",
        {"organization_id": organization_id, "endpoint": endpoint, "rows": rows},
    )


def _log_action(request, organization_id, endpoint, action):
    from django.core.exceptions import ValidationError

    try:
        with transaction.atomic():
            action()
    except ConcurrentUpdate:
        messages.error(request, gettext("O webhook mudou. Atualize a página antes de tentar novamente."))
    except ValidationError:
        messages.error(request, gettext("O webhook está desativado: reative-o antes de reenviar ou testar."))
    else:
        messages.success(request, gettext("Entrega enfileirada."))
    return redirect("integrations:webhook-deliveries", organization_id=organization_id, sysid=endpoint.pk)


@integrations_page
@require_POST
@transaction.atomic
def delivery_resend(request, organization_id, sysid, delivery_id):
    from .log import resend_delivery
    from .models import WebhookDelivery

    endpoint = _endpoint(sysid)
    delivery = WebhookDelivery.objects.filter(pk=delivery_id, endpoint=endpoint).first()
    if delivery is None:
        raise Http404
    return _log_action(
        request,
        organization_id,
        endpoint,
        lambda: resend_delivery(delivery, actor=request.user, expected_revision=_revision(request)),
    )


@integrations_page
@require_POST
@transaction.atomic
def endpoint_ping(request, organization_id, sysid):
    from .log import ping_endpoint

    endpoint = _endpoint(sysid)
    return _log_action(
        request,
        organization_id,
        endpoint,
        lambda: ping_endpoint(endpoint, actor=request.user, expected_revision=_revision(request)),
    )
