"""While a rule demands MFA and the person has not enrolled it, only the MFA pages answer (#740).

Runs inside the request context opened by ``TenantContextMiddleware``, so the organization of
the request is the validated one (never a header, body or query value). Deny by default: any
route outside ``ALLOWED_ROUTES`` sends an HTML request to ``mfa_index`` and answers an API
request with 403 ``mfa_required``, also with a personal API token. Templates read
``request.mfa_enrollment_pending`` to hide the application's menus.

An organization's policy applies inside that organization, but once it held a browser session
back, the session stays on the MFA pages, global ones included, until the TOTP exists or the
person logs out.
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


# Set once an organization's policy held a browser session back: the MFA pages are global, so
# without it the enrolment would show the full application again, and the person could walk to
# another page. The platform's rules need no mark: they hold every request on their own.
SESSION_KEY = "skeleton_mfa_pending"


def _pending(request) -> bool:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    session = getattr(request, "session", None)
    # A bearer token has no browser session to mark; each request is judged on its own context.
    browser = session is not None and getattr(request, "api_token", None) is None
    demand = mfa_demand(user)
    marked = browser and bool(session.get(SESSION_KEY))
    if demand is None and not marked:
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
        pending = _pending(request)
        request.mfa_enrollment_pending = pending
        if pending and not allowed(request.path_info):
            if request.path_info.startswith("/api/"):
                return JsonResponse(
                    {"code": "mfa_required", "detail": gettext("Configure autenticação multifator para continuar.")},
                    status=403,
                )
            return redirect("mfa_index")
        return self.get_response(request)
