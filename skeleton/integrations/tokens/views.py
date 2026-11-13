"""Token pages (#47): ``/profile/tokens/`` and ``/o/<org>/integrations/service-accounts/``.

Only a session reaches them (HTML ignores Bearer headers). Every POST needs CSRF and a
recent authentication. The token text is rendered once, on a page that is never cached;
lists show prefix, name, scopes, validity and last use, never the secret.
"""

from __future__ import annotations

from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.cache import add_never_cache_headers, never_cache
from django.views.decorators.http import require_http_methods, require_POST

from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.integrations.webhooks.views import integrations_page
from skeleton.tenancy.context import current_context

from . import services
from .models import ApiToken
from .scopes import named_scopes, table_scopes

TEMPLATES = "skeleton/integrations/tokens/"


def scope_choices(*, feed: bool = False):
    named = [(scope.name, f"{scope.name} — {scope.description}") for scope in named_scopes() if scope.feed == feed]
    tables = [] if feed else [(scope, scope) for scope in table_scopes()]
    return tables + named


class TokenForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=150)
    scopes = forms.MultipleChoiceField(label=gettext_lazy("Escopos"), widget=forms.CheckboxSelectMultiple)
    days = forms.IntegerField(label="Validade (dias)", min_value=1, initial=90)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scopes"].choices = scope_choices()
        self.fields["days"].widget.attrs["max"] = services.max_days()
        self.fields["days"].help_text = gettext("No máximo %(value1)s dias.") % {"value1": services.max_days()}

    def clean_days(self):
        days = self.cleaned_data["days"]
        if days > services.max_days():
            raise forms.ValidationError(gettext("No máximo %(value1)s dias.") % {"value1": services.max_days()})
        return days

    def expires_at(self):
        return timezone.now() + timedelta(days=self.cleaned_data["days"])


class RevisionForm(forms.Form):
    revision = forms.IntegerField(min_value=1)


def _apply_error(form, error):
    for field, problems in error.message_dict.items():
        form.add_error("days" if field == "expires_at" else field if field in form.fields else None, problems)
    return 400


def _token_page(request, issued, *, done_url):
    response = render(
        request, TEMPLATES + "issued.html", {"token": issued.token, "text": issued.text, "done_url": done_url}
    )
    response.status_code = 201
    add_never_cache_headers(response)
    return response


def _rows(tokens, revoke_url):
    """Tokens for the table: never the digest, plus the revoke URL of each row."""
    from .activity import last_used

    rows = list(tokens)
    used = last_used(token.pk for token in rows)
    now = timezone.now()
    for token in rows:
        token.revoke_url = revoke_url(token)
        token.last_used_at = used.get(token.pk)
        token.usable = services.is_usable(token, now=now)  # Shown as "Inválido" when it no longer authenticates.
    return rows


# --------------------------------------------------------------------------- personal tokens


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def personal_tokens(request):
    if request.user.account_type != "human":
        raise Http404
    form = TokenForm(request.POST or None)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    issued = services.issue_personal_token(
                        actor=request.user,
                        name=form.cleaned_data["name"],
                        scopes=form.cleaned_data["scopes"],
                        expires_at=form.expires_at(),
                    )
            except ValidationError as exc:
                status = _apply_error(form, exc)
            except PermissionDenied:
                return redirect("mfa_index")
            else:
                from django.urls import reverse

                return _token_page(request, issued, done_url=reverse("integrations:personal-tokens"))
        else:
            status = 400
    from django.urls import reverse

    tokens = _rows(
        services.personal_tokens(request.user),
        lambda token: reverse("integrations:personal-token-revoke", args=[token.pk]),
    )
    context = {"form": form, "tokens": tokens, "now": timezone.now()}
    return render(request, TEMPLATES + "personal.html", context, status=status)


@login_required
@never_cache
@require_POST
@recent_authentication_required
@transaction.atomic
def personal_token_revoke(request, sysid):
    form = RevisionForm(request.POST)
    if not form.is_valid():
        raise Http404
    try:
        with transaction.atomic():
            services.revoke_token(actor=request.user, token_id=sysid, expected_revision=form.cleaned_data["revision"])
    except ApiToken.DoesNotExist:
        raise Http404 from None
    except ConcurrentUpdate:
        messages.error(request, gettext("O token mudou. Atualize a página antes de tentar novamente."))
        return redirect("integrations:personal-tokens")
    messages.success(request, gettext("Token revogado."))
    return redirect("integrations:personal-tokens")


# --------------------------------------------------------------------------- service accounts


class ServiceAccountForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome"), max_length=150)


def _accounts():
    from skeleton.accounts.models import AccountType, User

    return User.objects.filter(
        account_type=AccountType.SERVICE, service_organization_id=current_context().organization_id
    ).order_by("-is_active", "first_name", "sysid")


def _account(sysid):
    account = _accounts().filter(pk=sysid).first()
    if account is None:
        raise Http404
    return account


@integrations_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def service_accounts(request, organization_id):
    from skeleton.accounts.services import create_service_account

    form = ServiceAccountForm(request.POST or None)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    account = create_service_account(actor=request.user, name=form.cleaned_data["name"])
            except ValidationError as exc:
                status = _apply_error(form, exc)
            else:
                messages.success(request, gettext("Conta de serviço criada."))
                return redirect("integrations:service-account", organization_id=organization_id, sysid=account.pk)
        else:
            status = 400
    from skeleton.web.views import pagination

    rows = pagination(request, _accounts())
    return render(
        request,
        TEMPLATES + "service_accounts.html",
        {"organization_id": organization_id, "accounts": rows, "rows": rows, "form": form},
        status=status,
    )


@integrations_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def service_account(request, organization_id, sysid):
    from django.urls import reverse

    account = _account(sysid)
    form = TokenForm(request.POST or None)
    status = 200
    if request.method == "POST":
        if form.is_valid() and account.is_active:
            try:
                with transaction.atomic():
                    issued = services.issue_service_token(
                        actor=request.user,
                        account=account,
                        name=form.cleaned_data["name"],
                        scopes=form.cleaned_data["scopes"],
                        expires_at=form.expires_at(),
                    )
            except ValidationError as exc:
                status = _apply_error(form, exc)
            except PermissionDenied:
                return redirect("mfa_index")
            else:
                done = reverse(
                    "integrations:service-account", kwargs={"organization_id": organization_id, "sysid": sysid}
                )
                return _token_page(request, issued, done_url=done)
        else:
            status = 400
    tokens = _rows(
        services.service_tokens(organization_id, account=account),
        lambda token: reverse(
            "integrations:service-token-revoke",
            kwargs={"organization_id": organization_id, "sysid": sysid, "token_id": token.pk},
        ),
    )
    return render(
        request,
        TEMPLATES + "service_account.html",
        {"organization_id": organization_id, "account": account, "tokens": tokens, "form": form, "now": timezone.now()},
        status=status,
    )


@integrations_page
@require_POST
@recent_authentication_required
@transaction.atomic
def service_account_deactivate(request, organization_id, sysid):
    from skeleton.accounts.services import deactivate_service_account

    deactivate_service_account(actor=request.user, user=_account(sysid))
    messages.success(request, gettext("Conta de serviço desativada; os tokens dela deixaram de valer."))
    return redirect("integrations:service-account", organization_id=organization_id, sysid=sysid)


@integrations_page
@require_POST
@recent_authentication_required
@transaction.atomic
def service_token_revoke(request, organization_id, sysid, token_id):
    account = _account(sysid)
    form = RevisionForm(request.POST)
    if (
        not form.is_valid()
        or not services.service_tokens(organization_id, account=account).filter(pk=token_id).exists()
    ):
        raise Http404
    try:
        with transaction.atomic():
            services.revoke_token(
                actor=request.user, token_id=token_id, expected_revision=form.cleaned_data["revision"]
            )
    except ApiToken.DoesNotExist:
        raise Http404 from None
    except ConcurrentUpdate:
        messages.error(request, gettext("O token mudou. Atualize a página antes de tentar novamente."))
    else:
        messages.success(request, gettext("Token revogado."))
    return redirect("integrations:service-account", organization_id=organization_id, sysid=sysid)
