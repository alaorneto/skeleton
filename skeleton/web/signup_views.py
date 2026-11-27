"""The self-service signup of an organization (#715) at /organizations/new/: 404 while it is off."""

from __future__ import annotations

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods, require_safe

from skeleton.tenancy import signup

from .slug_check import slug_check_attrs, slug_check_response


class SignupForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Nome da organização"), max_length=200)
    slug = forms.SlugField(
        label=gettext_lazy("Identificador"),
        max_length=63,
        help_text=gettext_lazy(
            "Letras minúsculas, dígitos e hífen; aparece no endereço. O administrador pode trocá-lo depois."
        ),
    )
    bundle = forms.ChoiceField(label=gettext_lazy("Pacote"), choices=(), widget=forms.RadioSelect)
    accept_terms = forms.BooleanField(
        label=gettext_lazy("Aceito os termos de contratação do pacote escolhido"), required=False
    )

    def __init__(self, *args, bundles=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bundle"].choices = [(item.code, item.label) for item in bundles]
        slug_check_attrs(self.fields["slug"], url=reverse("web:organization-signup-slug"))


def _members_line(bundle) -> str:
    """How many members the bundle brings, from the platform's per-app amounts (#696)."""
    from skeleton.common.limits import combine
    from skeleton.system.bundles import bundle_apps
    from skeleton.system.limits import MEMBERS, base_for
    from skeleton.system.models import AppAvailability

    amounts = [
        int((limits or {}).get(MEMBERS, 0) or 0)
        for limits in AppAvailability.objects.filter(app_label__in=bundle_apps(bundle)).values_list("limits", flat=True)
    ]
    limit = combine(base_for(MEMBERS), [amount for amount in amounts if amount])
    if limit is None:
        return gettext("Membros sem limite")
    return gettext("Inclui até %(limit)s membros") % {"limit": limit}


@login_required
@require_http_methods(["GET", "POST"])
def organization_signup(request):
    if not signup.enabled():
        raise Http404
    bundles = signup.signup_bundles()
    form = SignupForm(request.POST or None, bundles=bundles)
    status = 200
    unavailable = signup.unavailable_reason()
    if request.method == "POST" and unavailable is None:
        if form.is_valid():
            data = form.cleaned_data
            try:
                result = signup.signup_organization(
                    user=request.user,
                    name=data["name"],
                    slug=data["slug"],
                    bundle_code=data["bundle"],
                    accept_terms=data["accept_terms"],
                )
            except PermissionDenied as exc:
                form.add_error(None, str(exc))
                status = 403
            except ValidationError as exc:
                from skeleton.web.forms import form_error

                form_error(form, exc)
                status = 400
            else:
                home = reverse("web:dashboard", kwargs={"organization_id": result.organization.pk})
                if result.invoice_id is not None:
                    return _checkout(request, result, home)
                return redirect(home)
        else:
            status = 400
    elif request.method == "POST":
        status = 409
    return render(
        request,
        "skeleton/web/signup.html",
        {
            "form": form,
            "bundles": [{"bundle": item, "members": _members_line(item)} for item in bundles],
            "unavailable": unavailable,
            "remaining": signup.remaining(request.user),
        },
        status=status,
    )


@login_required
@require_safe
def organization_signup_slug(request):
    """Whether the typed identifier is free (#765); closed like the signup page."""
    if not signup.enabled():
        raise Http404
    return slug_check_response(request)


def _checkout(request, result, home):
    """A paid bundle: the hosted checkout of the first invoice, opened after the commit."""
    from skeleton.service_api.billing import BillingService
    from skeleton.tenancy.context import tenant_context

    try:
        with tenant_context(organization_id=result.organization.pk, actor=request.user, source="http:signup"):
            link = BillingService().checkout(
                actor=request.user, invoice_id=result.invoice_id, return_url=request.build_absolute_uri(home)
            )
    except Exception:  # noqa: BLE001 - the organization exists; the person pays from the billing page.
        return redirect(home)
    return redirect(link.url)
