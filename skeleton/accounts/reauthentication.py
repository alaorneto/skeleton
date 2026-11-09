"""Recent-authentication rule for sensitive actions.

The core counts the latest login or reauthentication recorded in this session, for the same
user, within ``ACCOUNT_REAUTHENTICATION_TIMEOUT``. allauth 65.21 changed its own check to count
only explicit reauthentication, so sensitive views use these helpers instead of allauth's
``reauthentication_required`` decorator. Raising ``ReauthenticationRequired`` still hands the
request to allauth's reauthentication flow.
"""

import time
from functools import wraps

from allauth.account import app_settings as account_settings
from allauth.account.authentication import get_authentication_records
from allauth.core.exceptions import ReauthenticationRequired

__all__ = ["has_recent_authentication", "recent_authentication_required", "require_recent_authentication"]


def has_recent_authentication(request, *, allow_without_reauthentication_flow=False):
    """Whether the session authenticated this user recently.

    ``allow_without_reauthentication_flow`` keeps allauth's fallback for accounts that have no
    way to reauthenticate locally (SSO-only, no MFA). Use it only where a dead-end redirect to
    the reauthentication page would be worse than accepting an older login.
    """
    user = request.user
    if not user.is_authenticated or not user.is_active:
        return False
    if allow_without_reauthentication_flow:
        from allauth.account.internal.flows.reauthentication import get_reauthentication_flows

        if not get_reauthentication_flows(user):
            return True
    records = get_authentication_records(request, user)
    return bool(records) and time.time() - records[-1]["at"] < account_settings.REAUTHENTICATION_TIMEOUT


def require_recent_authentication(request):
    if not has_recent_authentication(request):
        raise ReauthenticationRequired()


def recent_authentication_required(view=None, *, allow_get=False):
    """Require recent authentication; ``allow_get`` lets safe GET requests render the form."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not (allow_get and request.method == "GET"):
                require_recent_authentication(request)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator(view) if view is not None else decorator
