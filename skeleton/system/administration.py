"""Application administration is derived from the active, code-owned registry."""
from pathlib import Path

from allauth.account.decorators import reauthentication_required
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import render
from django.urls import path, resolve, reverse
from django.views.decorators.http import require_safe

from skeleton.access.services import require_organization_admin
from skeleton.tenancy.context import current_context, memberships_for_user
from .app_registry import all_apps, get_app
from .applications import require_application_enabled


def application_manifest(app_label):
    try:
        return get_app(app_label)
    except KeyError:
        raise Http404 from None


def application_identity(manifest):
    """Never derive release identity from editable catalog descriptions."""
    return {
        'app_id': manifest.app_id or manifest.app_label,
        'app_label': manifest.app_label, 'label': manifest.label,
        'description': manifest.description, 'version': manifest.version,
        'origin': manifest.origin,
        'compatibility': (manifest.canonical or {}).get('compatibility', {}),
        'dependencies': manifest.dependencies,
    }


def resolve_application_screen(app_label, screen_id, *, user, scope):
    """Shared policy for discovery, dispatch, and app-owned direct URLs."""
    manifest = application_manifest(app_label)
    screen = next((item for item in manifest.administration.screens if item.id == screen_id), None)
    if screen is None or screen.scope != scope:
        raise Http404
    if scope == 'platform':
        from .releases import require_platform
        require_platform(user)
    else:
        require_organization_admin(user)
    return manifest, screen


def screen_sections(manifest, *, user, scope):
    entries = {}
    for screen in sorted(manifest.administration.screens, key=lambda item: (item.order, item.id)):
        if screen.scope != scope:
            continue
        try:
            resolve_application_screen(manifest.app_label, screen.id, user=user, scope=scope)
        except PermissionDenied:
            continue
        url = reverse('admin:skeleton_app_screen', kwargs={'app_label': manifest.app_label, 'screen_id': screen.id}) if scope == 'platform' else reverse(
            'application-admin-screen', kwargs={'organization_id': current_context().organization_id,
                                               'app_label': manifest.app_label, 'screen_id': screen.id})
        entries.setdefault(screen.section_id, []).append({'id': screen.id, 'label': screen.label, 'url': url})
    return [{'id': section.id, 'label': section.label, 'screens': entries[section.id]}
            for section in sorted(manifest.administration.sections, key=lambda item: (item.order, item.id))
            if section.id in entries]


def guard_application_screen(request, view_name, *, app_label=None):
    """Parameter screens require tenant administration; other app routes require activation."""
    if app_label is not None:
        manifest = application_manifest(app_label)
    else:
        namespace = view_name.split(':', 1)[0]
        try:
            manifest = get_app(namespace)
        except KeyError:
            return
    for screen in manifest.administration.screens:
        if screen.scope == 'organization' and screen.kind == 'custom' and screen.url_name == view_name:
            resolve_application_screen(manifest.app_label, screen.id, user=request.user, scope='organization')
            return
    require_application_enabled(manifest.app_label)


def guard_application_urls(patterns, manifest):
    """Clone the URL tree so module-owned URLPattern objects stay untouched."""
    from functools import wraps
    from django.urls.resolvers import URLPattern, URLResolver

    result = []
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            result.append(URLResolver(
                pattern.pattern, guard_application_urls(pattern.url_patterns, manifest),
                default_kwargs=pattern.default_kwargs, app_name=pattern.app_name,
                namespace=pattern.namespace,
            ))
        elif isinstance(pattern, URLPattern):
            def protect(callback):
                @wraps(callback)
                def guarded(request, *args, **kwargs):
                    guard_application_screen(request, request.resolver_match.view_name,
                                             app_label=manifest.app_label)
                    return callback(request, *args, **kwargs)
                return guarded
            result.append(URLPattern(pattern.pattern, protect(pattern.callback),
                                     default_args=pattern.default_args, name=pattern.name))
        else:
            raise TypeError(f'Unsupported application URL declaration: {manifest.app_label}.')
    return result




def tenant_screen_url(manifest, screen, organization_id):
    if screen.kind == 'configuration':
        return reverse('application-configuration', kwargs={'organization_id': organization_id, 'app_label': manifest.app_label})
    if not screen.url_name or screen.url_name.split(':', 1)[0] != manifest.app_label:
        raise Http404
    url = reverse(screen.url_name, kwargs={'organization_id': organization_id})
    prefix = f'/o/{organization_id}/apps/{manifest.app_label}/'
    if not url.startswith(prefix) or url.startswith('//') or resolve(url).view_name != screen.url_name:
        raise Http404
    return url


def platform_application_groups():
    """Django's index and sidebar consume the same ephemeral application groups."""
    result = []
    for manifest in all_apps():
        overview = reverse('admin:skeleton_app_group', kwargs={'app_label': manifest.app_label})
        links = [
            ('Visão geral', overview),
            ('Configuração por organização', reverse('admin:skeleton_app_organizations', kwargs={'app_label': manifest.app_label})),
            ('Documentação', reverse('admin:skeleton_app_documentation', kwargs={'app_label': manifest.app_label})),
        ]
        for screen in sorted(manifest.administration.screens, key=lambda item: (item.order, item.id)):
            name = 'admin:skeleton_app_screen' if screen.scope == 'platform' else 'admin:skeleton_app_organizations'
            url = reverse(name, kwargs={'app_label': manifest.app_label, **({'screen_id': screen.id} if screen.scope == 'platform' else {})})
            if screen.scope == 'organization':
                from urllib.parse import urlencode
                url += '?' + urlencode({'screen': screen.id})
            links.append((screen.label, url))
        result.append({'name': manifest.label, 'app_label': f'application__{manifest.app_label}',
                       'app_id': manifest.app_id or manifest.app_label, 'app_url': overview,
                       'has_module_perms': True,
                       'models': [{'name': label, 'object_name': f'screen_{index}', 'admin_url': url,
                                   'perms': {'view': True}, 'view_only': True}
                                  for index, (label, url) in enumerate(links)]})
    return result


def _platform_context(request, manifest, **values):
    return {**admin.site.each_context(request), 'title': manifest.label,
            'application': application_identity(manifest), **values}


@require_safe
def platform_group(request, app_label):
    manifest = application_manifest(app_label)
    return render(request, 'admin/skeleton/application.html', _platform_context(
        request, manifest, sections=screen_sections(manifest, user=request.user, scope='platform'),
        tenant_screens=sorted((screen for screen in manifest.administration.screens if screen.scope == 'organization'),
                              key=lambda item: (item.order, item.id))))


@require_safe
def platform_organizations(request, app_label):
    manifest = application_manifest(app_label)
    screen_id = request.GET.get('screen')
    if screen_id and not any(screen.id == screen_id and screen.scope == 'organization' for screen in manifest.administration.screens):
        raise Http404
    memberships = memberships_for_user(request.user)
    organizations = []
    for membership in memberships:
        name = 'application-admin-screen' if screen_id else 'application-configuration'
        kwargs = {'organization_id': membership['organization_id'], 'app_label': app_label}
        if screen_id:
            kwargs['screen_id'] = screen_id
        organizations.append({'name': membership['organization_name'], 'url': reverse(name, kwargs=kwargs)})
    return render(request, 'admin/skeleton/application_organizations.html', _platform_context(
        request, manifest, organizations=organizations))


@require_safe
def platform_documentation(request, app_label):
    manifest = application_manifest(app_label)
    if not manifest.documentation_root:
        raise Http404
    root = Path(manifest.documentation_root).resolve()
    documents = []
    for filename in ('spec.md', 'adr.md'):
        source = root / filename
        if not source.resolve().is_relative_to(root) or not source.is_file():
            raise Http404
        documents.append({'name': filename, 'text': source.read_text(encoding='utf-8')})
    return render(request, 'admin/skeleton/application_documentation.html', _platform_context(
        request, manifest, documents=documents))


def platform_screen(request, app_label, screen_id):
    manifest, screen = resolve_application_screen(app_label, screen_id, user=request.user, scope='platform')
    if screen.kind != 'custom':
        raise Http404
    from .presentation import get_admin_handler
    try:
        handler = get_admin_handler(manifest.app_label, screen.handler_id)
    except KeyError:
        raise Http404 from None
    if screen.sensitive:
        from .release_admin import _recent_authentication
        @reauthentication_required(allow_get=True)
        def sensitive_handler(request):
            _recent_authentication(request)
            return handler(request)
        return sensitive_handler(request)
    return handler(request)


def urlpatterns(site):
    return [
        path('system/apps/<str:app_label>/', site.admin_view(platform_group), name='skeleton_app_group'),
        path('system/apps/<str:app_label>/screens/<str:screen_id>/', site.admin_view(platform_screen), name='skeleton_app_screen'),
        path('system/apps/<str:app_label>/organizations/', site.admin_view(platform_organizations), name='skeleton_app_organizations'),
        path('system/apps/<str:app_label>/documentation/', site.admin_view(platform_documentation), name='skeleton_app_documentation'),
    ]
