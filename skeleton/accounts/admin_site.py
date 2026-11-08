from functools import wraps

from allauth.account.decorators import secure_admin_login
from django.contrib.admin import AdminSite
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import include, path
from django.utils.decorators import method_decorator

from skeleton.access.services import LastAdministrator
from skeleton.accounts.mfa import mfa_required
from skeleton.common.exceptions import ConcurrentUpdate


class PlatformAdminSite(AdminSite):
    """Transitional host of the release screens; platform administration lives in /platform/.

    No model is registered here and no generic changelist/catch-all is routed: global
    records are edited through the explicit views of ``skeleton.web.platform``.
    """

    site_header = "Administração da plataforma"
    site_title = "Skeleton"
    index_title = "Administração da plataforma"

    def get_urls(self):
        from skeleton.system.release_admin import urlpatterns as release_urls

        kept = {"index", "login", "logout", "password_change", "password_change_done"}
        routes = release_urls(self) + [route for route in super().get_urls() if getattr(route, "name", None) in kept]
        return [path("admin/", include(routes))]

    def get_app_list(self, request, app_label=None):
        return []

    def index(self, request, extra_context=None):
        return redirect("platform:overview")

    def has_permission(self, request):
        user = request.user
        return bool(
            user.is_authenticated and user.is_active and user.deleted_at is None and user.is_staff and user.is_superuser
        )

    def admin_view(self, view, cacheable=False):
        wrapped = super().admin_view(view, cacheable=cacheable)

        @wraps(wrapped)
        def guarded(request, *args, **kwargs):
            if request.user.is_authenticated and not self.has_permission(request):
                raise PermissionDenied
            if self.has_permission(request) and mfa_required(request.user):
                return redirect("mfa_index")
            try:
                return wrapped(request, *args, **kwargs)
            except LastAdministrator:
                return render(
                    request,
                    "skeleton/web/error.html",
                    {"detail": "A operação deixaria uma organização sem administrador ativo."},
                    status=409,
                )
            except ConcurrentUpdate:
                return render(
                    request,
                    "skeleton/web/error.html",
                    {"detail": "O registro mudou. Atualize o formulário."},
                    status=409,
                )

        return guarded

    @method_decorator(secure_admin_login)
    def login(self, request, extra_context=None):
        if request.user.is_authenticated:
            if not self.has_permission(request):
                raise PermissionDenied
            return redirect("mfa_index" if mfa_required(request.user) else "platform:overview")
        return super().login(request, extra_context)
