"""DRF side of token authentication (#46): the middleware already verified the token."""

from rest_framework.authentication import BaseAuthentication


class ApiTokenAuthentication(BaseAuthentication):
    """First in ``SessionAPI``: a verified Bearer token authenticates without CSRF.

    ``ApiTokenMiddleware`` verifies the token, refuses views that do not accept tokens and sets
    ``request.api_token``; this class only hands DRF that identity, so ``SessionAuthentication``
    (and its CSRF check) never runs for a token request. Without a token it defers to the session.
    """

    def authenticate(self, request):
        token = getattr(request._request, "api_token", None)
        if token is None:
            return None
        return (request._request.user, token)
