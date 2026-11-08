"""Access guard shared by every platform administration view."""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from skeleton.access.services import LastAdministrator
from skeleton.accounts.mfa import mfa_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system.releases import require_platform

__all__ = ["has_platform_role", "platform_page"]


def has_platform_role(user):
    return bool(
        user.is_authenticated and user.is_active and user.deleted_at is None and user.is_staff and user.is_superuser
    )


def platform_page(view):
    """Anonymous → login; non-platform → 403; pending MFA → enrolment; global context only."""

    @never_cache
    @wraps(view)
    def guarded(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not has_platform_role(user):
            raise PermissionDenied("Platform administration required.")
        if mfa_required(user):
            return redirect("mfa_index")
        require_platform(user)
        try:
            return view(request, *args, **kwargs)
        except LastAdministrator:
            detail = "A operação deixaria uma organização sem administrador ativo."
        except ConcurrentUpdate:
            detail = "O registro mudou. Atualize a página antes de tentar novamente."
        return render(request, "skeleton/web/error.html", {"detail": detail}, status=409)

    return guarded
