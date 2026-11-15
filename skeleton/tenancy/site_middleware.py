"""Middlewares of the organization sites (#477): the shared session cookie and the host routing."""

from __future__ import annotations

from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import DisallowedHost

from .site import base_domain, shares_session


class SiteSessionMiddleware(SessionMiddleware):
    """Session cookie shared by every host under the base domain; host-only everywhere else (#481).

    The domain is decided per request: a browser accepts ``Domain=.<base>`` only from a host
    inside the base domain, and a custom domain (#489) must keep a host-only cookie. The CSRF
    cookie is never shared: each host gets its own and the ``Origin`` check stays per host.
    """

    def process_response(self, request, response):
        response = super().process_response(request, response)
        name = settings.SESSION_COOKIE_NAME
        if name in response.cookies:
            try:
                host = request.get_host()
            except DisallowedHost:
                return response
            if shares_session(host):
                response.cookies[name]["domain"] = "." + base_domain()
        return response
