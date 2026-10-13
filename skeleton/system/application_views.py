import re

from django import forms
from django.core.exceptions import ValidationError
from django.shortcuts import render, redirect
from django.http import Http404
from django.views.decorators.http import require_http_methods
from rest_framework.response import Response

from skeleton.api.views import TenantAPI, etag, request_fields
from skeleton.api.errors import BoundaryError
from skeleton.access.services import require_organization_admin
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.web.views import tenant_page
from .app_registry import all_apps, get_app
from .applications import application_state, configure_application


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
        'applications': [application_state(app.app_label) for app in all_apps()],
    })


@tenant_page(admin=True)
@require_http_methods(['GET', 'POST'])
def application_configuration(request, organization_id, app_label):
    manifest = _manifest(app_label)
    state = application_state(app_label)
    fields = {'enabled': forms.BooleanField(required=False),
              'revision': forms.IntegerField(min_value=0, widget=forms.HiddenInput)}
    for field in manifest.configuration_fields:
        if field.value_type is str:
            fields[field.name] = forms.CharField(max_length=field.max_length, required=False)
        elif field.value_type is bool:
            fields[field.name] = forms.BooleanField(required=False)
        else:
            fields[field.name] = forms.IntegerField()
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
                return redirect('application-list', organization_id=organization_id)
            except ConcurrentUpdate:
                form.add_error(None, 'A configuração mudou. Atualize a página.')
                status = 409
            except ValidationError:
                form.add_error(None, 'Verifique os campos e as dependências ativas.')
                status = 400
        else:
            status = 400
    return render(request, 'skeleton/web/form.html', {'title': manifest.label, 'form': form, 'organization_id': organization_id}, status=status)
