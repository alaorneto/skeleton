"""Late phase of the administrative maintenance mode (#447): /platform/ for full administrators only.

The early ``skeleton.deployment.MaintenanceMiddleware`` lets the routes of the platform namespace
through and marks the request with ``skeleton_maintenance``. Here, after authentication, only a
full platform administrator (``is_platform_admin``) goes on. Everyone else, the delegated
platform roles included, gets the maintenance answer: a 503, never a 403 that would reveal the
administrative surface.
"""

from __future__ import annotations

from skeleton.accounts.platform import is_platform_admin
from skeleton.common.maintenance_response import maintenance_response, platform_login_url

PLATFORM_PREFIX = "/platform/"
MIDDLEWARE = "skeleton.web.maintenance.MaintenancePlatformGuardMiddleware"


class MaintenancePlatformGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        state = getattr(request, "skeleton_maintenance", None)
        if (
            state is not None
            and request.path_info.startswith(PLATFORM_PREFIX)
            and not is_platform_admin(getattr(request, "user", None))
        ):
            return maintenance_response(
                request,
                message=state.message,
                ends_at=state.ends_at,
                retry_after=state.retry_after(),
                login_url=platform_login_url(),
            )
        return self.get_response(request)
