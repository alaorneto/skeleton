import re

from django import forms
from django.core.exceptions import ValidationError
from django.shortcuts import render, redirect
from django.http import Http404
from django.views.decorators.http import require_http_methods, require_safe
from rest_framework.response import Response

from skeleton.api.views import TenantAPI, etag, request_fields
from skeleton.api.errors import BoundaryError
from skeleton.access.services import require_organization_admin
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.web.views import tenant_page
from .app_registry import all_apps, get_app
from .applications import application_state, configure_application
from .administration import (
    application_identity, resolve_application_screen, screen_sections, tenant_screen_url,
)


def _manifest(app_label):
    try:
        return get_app(app_label)
    except KeyError:
        raise Http404 from None


class ApplicationAPI(TenantAPI):
    def check_permissions(self, request):
        super().check_permissions(request)
        require_organization_admin(request.user)

    def get(self, request, organization_id, app_label=None):
        if app_label is None:
            return Response({'results': [application_state(app.app_label) for app in all_apps()]})
        _manifest(app_label)
        state = application_state(app_label)
        return etag(Response(state), state['revision'])

    def put(self, request, organization_id, app_label):
        _manifest(app_label)
        value = request.headers.get('If-Match')
        if value is None:
            raise BoundaryError('revision_required', 'Informe a revisão.', 428)
        if not re.fullmatch(r'"(?:0|[1-9][0-9]{0,18})"', value):
            raise BoundaryError('invalid_revision', 'Revisão inválida.')
        data = request_fields(request)
        if set(data) != {'enabled', 'configuration'}:
            raise BoundaryError('invalid_request', 'Informe enabled e configuration.')
        configure_application(app_label, user=request.user, revision=int(value[1:-1]), **data)
        state = application_state(app_label)
        return etag(Response(state), state['revision'])


@tenant_page(admin=True)
def application_list(request, organization_id):
    return render(request, 'skeleton/web/applications.html', {
        'organization_id': organization_id,
        'parameter_panel': True,
        'applications': [application_state(app.app_label) for app in all_apps()],
    })


@tenant_page(admin=True)
@require_safe
def application_admin(request, organization_id, app_label):
    manifest = _manifest(app_label)
    state = application_state(app_label)
    return render(request, 'skeleton/web/application_admin.html', {
        'organization_id': organization_id, 'application': application_identity(manifest),
        'parameter_panel': True,
        'state': state,
        'sections': screen_sections(manifest, user=request.user, scope='organization'),
    })


@tenant_page(admin=True)
@require_safe
def application_admin_screen(request, organization_id, app_label, screen_id):
    manifest, screen = resolve_application_screen(app_label, screen_id, user=request.user, scope='organization')
    return redirect(tenant_screen_url(manifest, screen, organization_id))


@tenant_page(admin=True)
@require_http_methods(['GET', 'POST'])
def application_configuration(request, organization_id, app_label):
    manifest = _manifest(app_label)
    state = application_state(app_label)
    fields = {'enabled': forms.BooleanField(required=False, label='Aplicativo ativo'),
              'revision': forms.IntegerField(min_value=0, widget=forms.HiddenInput)}
    for field in manifest.configuration_fields:
        options = {'label': field.label or field.name.replace('_', ' ').capitalize(),
                   'help_text': field.help_text}
        if field.value_type is str:
            fields[field.name] = forms.CharField(max_length=field.max_length, required=False, **options)
        elif field.value_type is bool:
            fields[field.name] = forms.BooleanField(required=False, **options)
        else:
            fields[field.name] = forms.IntegerField(**options)
    dependencies = [application_state(label) for label in manifest.dependencies]
    fields['enabled'].help_text = (
        'Desativar preserva os registros e a configuração. ' +
        ('Dependências: ' + ', '.join(f"{item['label']} ({'ativa' if item['enabled'] else 'desativada'})"
                                    for item in dependencies) + '.' if dependencies else 'Sem dependências.')
    )
    form_type = type('ApplicationConfigurationForm', (forms.Form,), fields)
    form = form_type(request.POST if request.method == 'POST' else None,
                     initial={'enabled': state['enabled'], 'revision': state['revision'], **state['configuration']})
    status = 200
    if request.method == 'POST':
        if set(request.POST) - (set(form.fields) | {'csrfmiddlewaretoken'}):
            form.add_error(None, 'Campo não permitido.')
            status = 400
        elif form.is_valid():
            data = dict(form.cleaned_data)
            enabled, revision = data.pop('enabled'), data.pop('revision')
            try:
                configure_application(app_label, user=request.user, enabled=enabled, configuration=data, revision=revision)
                return redirect('application-admin', organization_id=organization_id, app_label=app_label)
            except ConcurrentUpdate:
                form.add_error(None, 'A configuração mudou. Atualize a página.')
                status = 409
            except ValidationError:
                form.add_error(None, 'Verifique os campos e as dependências ativas.')
                status = 400
        else:
            status = 400
    def section(identifier, label, names):
        return {'id': identifier, 'label': label, 'columns': 1, 'tab_id': None,
                'fields': [{'bound': form[name], 'label': form[name].label,
                            'help_text': form[name].help_text, 'read_only': False, 'col_span': 12}
                           for name in names]}
    sections = [section('application-activation', 'Ativação', ['enabled'])]
    for group in sorted(manifest.administration.configuration_sections, key=lambda item: (item.order, item.id)):
        names = [field.name for field in manifest.configuration_fields if field.section_id == group.id]
        if names:
            sections.append(section(group.id, group.label, names))
    unassigned = [field.name for field in manifest.configuration_fields if field.section_id is None]
    if unassigned:
        sections.append(section('application-configuration', 'Configuração', unassigned))
    from django.urls import reverse
    return render(request, 'skeleton/web/form.html', {
        'title': f'{manifest.label} — Configuração', 'form': form, 'sections': sections,
        'organization_id': organization_id,
        'parameter_panel': True,
        'breadcrumbs': [
            {'label': 'Apps', 'url': reverse('application-list', kwargs={'organization_id': organization_id})},
            {'label': manifest.label, 'url': reverse('application-admin', kwargs={'organization_id': organization_id, 'app_label': app_label})},
            {'label': 'Configuração'},
        ],
        'configuration_dependencies': manifest.dependencies,
    }, status=status)
