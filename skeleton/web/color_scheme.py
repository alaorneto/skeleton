"""The colour scheme of each page: light, dark or the system's (#665, #666).

The person's profile is the source of truth (``UserProfile.color_scheme``: ``""`` follows the
system, ``"light"`` or ``"dark"``). The cookie ``sk_color_scheme`` mirrors it for the pages before
login and after logout in the same browser: anonymous requests read only the cookie, and any other
value than ``light``/``dark`` counts as the system. The server renders the choice as
``<html data-color-scheme="light|dark">`` (absent for the system), so there is no script and no
flash; the theme stylesheet gets ``&scheme=`` (``skeleton.appearance.color_scheme``).

The cookie carries no identifier, only the enum. It is ``HttpOnly`` and ``SameSite=Lax``,
``Secure`` like the session cookie, and shared by the hosts of the base domain by the same rule
as the session (``shares_session``); a custom domain keeps a host-only cookie. Logout keeps it,
so the login page keeps the person's choice.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.utils.cache import patch_vary_headers

from skeleton.appearance.color_scheme import SCHEMES

__all__ = [
    "CHOICES",
    "COOKIE",
    "ColorSchemeMiddleware",
    "SCHEMES",
    "profile_scheme",
    "resolve",
    "scheme_value",
    "store_cookie",
    "with_scheme",
]

COOKIE = "sk_color_scheme"
MAX_AGE = 365 * 24 * 60 * 60
CHOICES = ("", *SCHEMES)


def scheme_value(value: Any) -> str:
    """``light``/``dark``, or ``""`` (the system) for anything else; never the raw value."""
    return value if isinstance(value, str) and value in SCHEMES else ""


def profile_scheme(user: Any) -> str | None:
    """The scheme of an authenticated person's profile (``""`` = system), or None without one."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        profile = user.profile
    except Exception:  # noqa: BLE001 - a person without a profile row simply has no preference.
        return None
    return scheme_value(getattr(profile, "color_scheme", ""))


def resolve(request: Any) -> str:
    """The scheme of this request: the profile when signed in, else the cookie, else the system."""
    chosen = profile_scheme(getattr(request, "user", None))
    if chosen is not None:
        return chosen
    return scheme_value(request.COOKIES.get(COOKIE))


def with_scheme(url: str, scheme: str) -> str:
    """``url`` with ``scheme=<scheme>`` when the scheme is forced; unchanged for the system."""
    if not url or scheme not in SCHEMES:
        return url
    return f"{url}{'&' if '?' in url else '?'}scheme={scheme}"


def _cookie_domain(request: Any) -> str | None:
    from skeleton.tenancy.site import base_domain, shares_session

    try:
        host = request.get_host()
    except DisallowedHost:
        return None
    return "." + base_domain() if shares_session(host) else None


def store_cookie(request: Any, response: Any, scheme: str) -> None:
    """Write the cookie for ``light``/``dark``; delete it for the system."""
    domain = _cookie_domain(request)
    if scheme in SCHEMES:
        response.set_cookie(
            COOKIE,
            scheme,
            max_age=MAX_AGE,
            path="/",
            domain=domain,
            secure=settings.SESSION_COOKIE_SECURE,
            httponly=True,
            samesite="Lax",
        )
    else:
        response.delete_cookie(COOKIE, path="/", domain=domain, samesite="Lax")


class ColorSchemeMiddleware:
    """Sets ``request.color_scheme`` and keeps the cookie in step with the signed-in person's profile.

    Sits after ``AuthenticationMiddleware`` (and the locale, which already loaded the profile, so
    no extra query). A response that already sets the cookie (the views that save the preference)
    is left alone; ``/api/`` never gets the cookie.
    """

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        request.color_scheme = resolve(request)
        response = self.get_response(request)
        if response.get("Content-Type", "").startswith("text/html"):
            patch_vary_headers(response, ("Cookie",))
        if COOKIE in response.cookies or request.path.startswith("/api/"):
            return response
        chosen = profile_scheme(getattr(request, "user", None))
        if chosen is not None and chosen != scheme_value(request.COOKIES.get(COOKIE)):
            if chosen or COOKIE in request.COOKIES:
                store_cookie(request, response, chosen)
        return response
