from functools import wraps

from allauth.account.decorators import secure_admin_login
from django.contrib.admin import AdminSite
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.template.response import TemplateResponse
from django.urls import include, path
from django.utils.decorators import method_decorator

from skeleton.accounts.mfa import mfa_required
from skeleton.access.services import LastAdministrator
from skeleton.common.exceptions import ConcurrentUpdate


class PlatformAdminSite(AdminSite):
    site_header = 'Configurações da plataforma'
    site_title = 'Skeleton'
    index_title = 'Configurações e parametrizações'
    index_template = 'admin/skeleton/index.html'

    def get_urls(self):
        from skeleton.system.release_admin import urlpatterns as release_urls
        from skeleton.system.administration import urlpatterns as application_urls

        configuration_names = {'index', 'login', 'logout', 'password_change', 'password_change_done'}
        configuration_urls = application_urls(self) + release_urls(self)
        record_urls = [path('', self.admin_view(self.records_index), name='records_index')]
        for route in super().get_urls():
            if getattr(route, 'name', None) in configuration_names:
                configuration_urls.append(route)
            else:
                record_urls.append(route)
        # Keep one namespace/site while scoping Django's app index and catch-all
        # to records, never to the application's unrelated root routes.
        return [
            path('admin/', include(configuration_urls)),
            path('platform/records/', include(record_urls)),
        ]

    def get_app_list(self, request, app_label=None):
        if not self.has_permission(request):
            return []
        from skeleton.system.administration import platform_application_groups
        return [group for group in platform_application_groups()
                if app_label is None or group['app_label'] == app_label]

    def get_record_app_list(self, request, app_label=None):
        return super().get_app_list(request, app_label)

    def each_context(self, request):
        context = super().each_context(request)
        if request.path_info.startswith('/platform/records/'):
            context.update(
                admin_base_template='skeleton/web/platform_records/base.html',
                available_apps=self.get_record_app_list(request),
                is_nav_sidebar_enabled=False,
            )
        return context

    def records_index(self, request, app_label=None, extra_context=None):
        context = self.each_context(request)
        app_list = (self.get_record_app_list(request, app_label)
                    if app_label is not None else context['available_apps'])
        if app_label is not None and not app_list:
            raise Http404
        context.update(
            title=app_list[0]['name'] if app_label is not None else 'Registros globais',
            subtitle=None,
            app_list=app_list,
            app_label=app_label,
        )
        context.update(extra_context or {})
        request.current_app = self.name
        return TemplateResponse(request, 'skeleton/web/platform_records/index.html', context)

    def app_index(self, request, app_label, extra_context=None):
        return self.records_index(request, app_label, extra_context)

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
