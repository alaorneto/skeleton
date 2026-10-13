from functools import wraps

from allauth.account.decorators import secure_admin_login
from django.contrib.admin import AdminSite
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator

from skeleton.accounts.mfa import mfa_required
from skeleton.access.services import LastAdministrator
from skeleton.common.exceptions import ConcurrentUpdate


class PlatformAdminSite(AdminSite):
    site_header = 'Administração da plataforma'
    site_title = 'Skeleton'
    index_template = 'admin/skeleton/index.html'

    def get_urls(self):
        from skeleton.system.release_admin import urlpatterns
        return urlpatterns(self) + super().get_urls()

    def has_permission(self, request):
        user = request.user
        return bool(user.is_authenticated and user.is_active and user.deleted_at is None and user.is_staff and user.is_superuser)

    def admin_view(self, view, cacheable=False):
        wrapped = super().admin_view(view, cacheable=cacheable)
        @wraps(wrapped)
        def guarded(request, *args, **kwargs):
            if request.user.is_authenticated and not self.has_permission(request):
                raise PermissionDenied
            if self.has_permission(request) and mfa_required(request.user):
                return redirect('mfa_index')
            try:
                return wrapped(request, *args, **kwargs)
            except LastAdministrator:
                return render(request, 'skeleton/web/error.html',
                              {'detail': 'A operação deixaria uma organização sem administrador ativo.'}, status=409)
            except ConcurrentUpdate:
                return render(request, 'skeleton/web/error.html',
                              {'detail': 'O registro mudou. Atualize o formulário.'}, status=409)
        return guarded

    @method_decorator(secure_admin_login)
    def login(self, request, extra_context=None):
        if request.user.is_authenticated:
            if not self.has_permission(request):
                raise PermissionDenied
            return redirect('mfa_index' if mfa_required(request.user) else 'admin:index')
        return super().login(request, extra_context)
