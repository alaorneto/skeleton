"""While a rule demands MFA and the person has not enrolled it, only the MFA pages answer (#740).

Runs inside the request context opened by ``TenantContextMiddleware``, so the organization of
the request is the validated one (never a header, body or query value). Deny by default: any
route outside ``ALLOWED_ROUTES`` sends an HTML request to ``mfa_index`` and answers an API
request with 403 ``mfa_required``, also with a personal API token. Templates read
``request.mfa_enrollment_pending`` to hide the application's menus.

An organization's policy holds back only that organization's pages: elsewhere (another
organization, the profile) the person moves freely until coming back. The MFA pages are global,
so a mark in the browser session (``SESSION_KEY``) keeps them without menus after that
organization sent the person there; it only changes how they look, never what answers.
"""

from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import Resolver404, resolve
from django.utils.translation import gettext

from .mfa import has_totp, mfa_demand

# What a person needs to finish the enrolment: the allauth MFA screens, the reauthentication
# they ask for, the email verification allauth requires before enrolling, and leaving.
ALLOWED_ROUTES = frozenset(
    {
        "mfa_index",
        "mfa_activate_totp",
        "mfa_authenticate",
        "mfa_reauthenticate",
        "mfa_view_recovery_codes",
        "mfa_generate_recovery_codes",
        "mfa_download_recovery_codes",
        "account_logout",
        "account_inactive",
        "account_reauthenticate",
        "account_email",
        "account_email_verification_sent",
        "account_confirm_email",
        # The instance's and the organization's brand, drawn by the reduced page itself.
        "web:favicon",
        "web:brand-theme",
        "web:brand-logo",
        "web:brand-logo-dark",
        "web:brand-icon",
        "web:brand-font",
        "web:brand-manifest",
        "web:site-logo",
        "web:site-icon",
        "web:site-manifest",
        "web:organization-logo",
        "web:organization-icon",
    }
)


def route_name(path: str) -> str | None:
    try:
        return resolve(path).view_name
    except Resolver404:
        return None


def allowed(path: str) -> bool:
    return route_name(path) in ALLOWED_ROUTES


# Set when an organization's rule sends a browser session to the MFA pages, which are global:
# while it lasts they keep hiding the menus. Presentation only: it never holds another page back,
# and it goes away on the first page outside the enrolment or once the TOTP exists.
SESSION_KEY = "skeleton_mfa_pending"


def _pending(request, *, enrolling: bool) -> bool:
    """Whether MFA is missing for this request; outside the enrolment, also whether to hold it back."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    session = getattr(request, "session", None)
    # A bearer token has no browser session to mark; each request is judged on its own context.
    browser = session is not None and getattr(request, "api_token", None) is None
    marked = browser and bool(session.get(SESSION_KEY))
    demand = mfa_demand(user)
    if demand is None and not (marked and enrolling):
        if marked:
            del session[SESSION_KEY]  # The person left the enrolment: other pages are free.
        return False
    if has_totp(user):
        if marked:
            del session[SESSION_KEY]
        return False
    if demand == "organization" and browser and not marked:
        session[SESSION_KEY] = True
    return True


class MFAEnrollmentMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        enrolling = allowed(request.path_info)
        pending = _pending(request, enrolling=enrolling)
        request.mfa_enrollment_pending = pending
        if pending and not enrolling:
            if request.path_info.startswith("/api/"):
                return JsonResponse(
                    {"code": "mfa_required", "detail": gettext("Configure autenticação multifator para continuar.")},
                    status=403,
                )
            return redirect("mfa_index")
        return self.get_response(request)
