from allauth.account.internal.decorators import login_not_required
from allauth.account.views import LogoutView
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.utils.http import urlencode
from django.views.decorators.http import require_http_methods

from skeleton.common.exceptions import QuotaLimitExceeded
from skeleton.tenancy.invitations import InvalidInvitation, accept_invitation, validate_invitation


@require_http_methods(["GET", "POST"])
def invitation_view(request, organization_id, token):
    try:
        proof = validate_invitation(organization_id, token)
    except (InvalidInvitation, ValidationError):
        return render(request, "account/invitation_invalid.html", status=400)
    request.session["skeleton_invitation"] = {
        "organization": str(organization_id),
        "invitation": str(proof.sysid),
        "digest": proof.token_digest,
    }
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect("account_signup")
        from skeleton.accounts.reauthentication import has_recent_authentication, reauthentication_url

        # SSO-only accounts cannot reauthenticate locally; their login is accepted as before.
        if not has_recent_authentication(request, allow_without_reauthentication_flow=True):
            return redirect(reauthentication_url(request.user) + "?next=" + request.path)
        try:
            accept_invitation(user=request.user, organization_id=organization_id, token=token)
        except QuotaLimitExceeded as full:
            # Reachable only when seats were taken by other paths after the invitation (#698).
            return render(
                request,
                "account/invitation.html",
                {"invitation_email": proof.email, "error": " ".join(full.messages)},
                status=409,
            )
        except ValidationError:
            return render(request, "account/invitation_invalid.html", status=400)
        request.session.pop("skeleton_invitation", None)
        return redirect("web:dashboard", organization_id=organization_id)
    return render(request, "account/invitation.html", {"invitation_email": proof.email})


@require_http_methods(["GET", "POST"])
def external_invitation_view(request, organization_id, token):
    """Accept an invitation to an organization's portal (#532): sign in (or sign up) with the invited email."""
    from skeleton.tenancy import external

    try:
        proof = external.validate(organization_id, token)
    except ValidationError:
        return render(request, "account/invitation_invalid.html", status=400)
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect(reverse_login(request.path))
        try:
            external.accept(
                user=request.user, organization_id=organization_id, token=token, ip=request.META.get("REMOTE_ADDR")
            )
        except external.TooManyAttempts:
            return render(request, "account/invitation_invalid.html", status=429)
        except ValidationError:
            # Another email, an unverified one, a plan limit: the same neutral answer.
            return render(request, "account/invitation_invalid.html", status=400)
        return render(request, "account/external_invitation_accepted.html", {"organization_id": organization_id})
    return render(request, "account/external_invitation.html", {"invitation_email": proof.email})


def reverse_login(next_path):
    from django.urls import reverse

    return reverse("account_login") + "?" + urlencode({"next": next_path})


@method_decorator(login_not_required, name="dispatch")
class ProviderLogoutView(LogoutView):
    """End the local allauth session before a reviewed browser logout redirect."""

    def get_redirect_url(self):
        from allauth.account.internal.flows.login import AUTHENTICATION_METHODS_SESSION_KEY

        from skeleton.accounts.providers import configured_providers, govbr_openid_configuration

        if self.request.method == "POST" and self.request.user.is_authenticated:
            methods = self.request.session.get(AUTHENTICATION_METHODS_SESSION_KEY, [])
            providers = [self.request.session.get("skeleton_govbr_logout_provider")]
            providers.extend(
                method.get("provider") for method in reversed(methods) if method.get("method") == "socialaccount"
            )
            for provider_id in providers:
                binding = configured_providers().get(provider_id)
                if binding is not None and binding.protocol == "govbr":
                    options = binding.app["settings"]
                    endpoint = govbr_openid_configuration(options["govbr_environment"])["end_session_endpoint"]
                    return (
                        endpoint
                        + "?"
                        + urlencode(
                            {
                                "post_logout_redirect_uri": options["govbr_logout_redirect_uri"],
                            }
                        )
                    )
        return super().get_redirect_url()


logout = ProviderLogoutView.as_view()
