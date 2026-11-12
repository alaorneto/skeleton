"""Access guard shared by every platform administration view."""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.utils.translation import gettext
from django.views.decorators.cache import never_cache

from skeleton.access.services import LastAdministrator
from skeleton.accounts.mfa import mfa_required
from skeleton.accounts.platform import LastPlatformAdministrator, is_platform_admin
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context

__all__ = ["has_platform_role", "platform_page"]


def has_platform_role(user):
    return is_platform_admin(user)


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def platform_page(view=None, *, capability=None, writes=False):
    """Anonymous → login; not allowed → 403; pending MFA → enrolment; global context only.

    Without ``capability`` only the platform administrator passes. With it, a limited
    platform role holding that capability (#34) also passes: for reads only, unless
    ``writes`` says the capability itself is about changing things (release operations).
    """

    def decorator(view):
        @never_cache
        @wraps(view)
        def guarded(request, *args, **kwargs):
            from skeleton.accounts.platform import has_platform_capability

            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            delegated = bool(
                capability and (writes or request.method in SAFE_METHODS) and has_platform_capability(user, capability)
            )
            if not (has_platform_role(user) or delegated):
                raise PermissionDenied("Platform administration required.")
            if mfa_required(user):
                return redirect("mfa_index")
            if current_context().organization_id is not None:
                raise PermissionDenied("Platform pages are global.")
            try:
                return view(request, *args, **kwargs)
            except LastPlatformAdministrator:
                detail = gettext("A operação deixaria a plataforma sem administrador ativo.")
            except LastAdministrator:
                detail = gettext("A operação deixaria uma organização sem administrador ativo.")
            except ConcurrentUpdate:
                detail = gettext("O registro mudou. Atualize a página antes de tentar novamente.")
            return render(request, "skeleton/web/error.html", {"detail": detail}, status=409)

        return guarded

    return decorator(view) if view is not None else decorator
