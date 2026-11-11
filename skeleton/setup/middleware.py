"""Setup mode routes every request to ``/setup/`` (health probes excepted)."""

from django.shortcuts import redirect

ALLOWED_PREFIXES = ("/setup/", "/health/", "/static/")


class SetupOnlyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(ALLOWED_PREFIXES):
            return redirect("/setup/")
        return self.get_response(request)
