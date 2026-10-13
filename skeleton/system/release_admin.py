import re
import tempfile
import time
from pathlib import Path

from allauth.account import app_settings as account_settings
from allauth.account.authentication import get_authentication_records
from allauth.account.decorators import reauthentication_required
from allauth.core.exceptions import ReauthenticationRequired
from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.core.files.uploadhandler import FileUploadHandler, StopUpload

from .models import ComponentInstallation, DeploymentOperation
from .releases import (
    agent_client, confirm_operation, create_preview, reconcile_operation,
    require_platform, retry_submission,
)


class UploadForm(forms.Form):
    package = forms.FileField(label='Pacote assinado (.skapp)')


class PlanForm(forms.Form):
    action = forms.ChoiceField(label='Operação', choices=(
        ('install', 'Instalar'), ('update', 'Atualizar'), ('uninstall', 'Desinstalar código'),
        ('reinstall', 'Reinstalar código'), ('core_update', 'Atualizar núcleo'),
    ))
    artifacts = forms.CharField(label='Digests dos pacotes', required=False,
                               widget=forms.Textarea(attrs={'rows': 2}),
                               help_text='Separe por espaço ou vírgula; permite atualização conjunta.')
    app_ids = forms.CharField(label='IDs das apps a desinstalar', required=False)

    def clean_artifacts(self):
        values = self.cleaned_data['artifacts'].replace(',', ' ').split()
        if len(values) > 32 or any(not re.fullmatch('[0-9a-f]{64}', value) for value in values):
            raise forms.ValidationError('Use digests SHA-256 retornados pelo upload.')
        return list(dict.fromkeys(values))

    def clean_app_ids(self):
        values = self.cleaned_data['app_ids'].replace(',', ' ').split()
        if len(values) > 32 or any(not re.fullmatch('[a-zA-Z0-9_.-]{1,100}', value) for value in values):
            raise forms.ValidationError('ID de app inválido.')
        return list(dict.fromkeys(values))

    def clean(self):
        values = super().clean()
        if values.get('action') == 'uninstall':
            if values.get('artifacts') or not values.get('app_ids'):
                raise forms.ValidationError('Desinstalação exige IDs de apps, sem pacotes.')
        elif not values.get('artifacts') or values.get('app_ids'):
            raise forms.ValidationError('Instalação/atualização exige pacotes, sem IDs de retirada.')
        return values


class ConfirmationForm(forms.Form):
    revision = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    plan_digest = forms.CharField(widget=forms.HiddenInput)


class BoundedPackageUpload(FileUploadHandler):
    def __init__(self, request):
        super().__init__(request)
        self.total = 0
        self.exceeded = False

    def receive_data_chunk(self, raw_data, start):
        self.total += len(raw_data)
        if self.total > getattr(settings, 'SKELETON_PACKAGE_MAX_BYTES', 128 * 1024 * 1024):
            self.exceeded = True
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None


def _recent_authentication(request):
    records = get_authentication_records(request)
    if not records or time.time() - records[-1]['at'] >= account_settings.REAUTHENTICATION_TIMEOUT:
        raise ReauthenticationRequired()


def _context(request, **values):
    return {**admin.site.each_context(request), 'title': 'Versões e pacotes', **values}


def _error(request, exc):
    messages.error(request, f'Operação indisponível: {getattr(exc, "code", type(exc).__name__)}.')


def index(request):
    require_platform(request.user)
    from skeleton.versioning import core_version
    inventory = None
    updates = None
    try:
        if request.method == 'POST' and request.POST.get('intent') == 'check':
            updates = agent_client().check_updates()
        inventory = agent_client().inventory()
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    return render(request, 'admin/skeleton/releases.html', _context(
        request, inventory=inventory, core=core_version(), updates=updates,
        upload_form=UploadForm(), plan_form=PlanForm(),
        components=ComponentInstallation.objects.order_by('kind', 'component_id'),
        operations=DeploymentOperation.objects.order_by('-created_at')[:30],
    ))


@csrf_exempt
def upload(request):
    # Install the bounded streaming handler before CSRF parses multipart data.
    request.upload_handlers.insert(0, BoundedPackageUpload(request))
    return _upload(request)


@csrf_protect
@reauthentication_required(allow_get=True)
def _upload(request):
    require_platform(request.user)
    if request.method != 'POST':
        return redirect('admin:skeleton_releases')
    _recent_authentication(request)
    handler = request.upload_handlers[0]
    form = UploadForm(request.POST, request.FILES)
    if handler.exceeded:
        return HttpResponse('Pacote excede o limite de upload.', status=413)
    if form.is_valid():
        try:
            uploaded = form.cleaned_data['package']
            with tempfile.TemporaryDirectory(prefix='skeleton-upload-') as directory:
                source = Path(directory) / 'upload.skapp'
                with source.open('xb') as stream:
                    for chunk in uploaded.chunks():
                        stream.write(chunk)
                source.chmod(0o600)
                artifact = agent_client().upload(source)
            messages.success(request, f'Pacote verificado. Digest: {artifact["digest"]}')
            return render(request, 'admin/skeleton/releases.html', _context(
                request, core=None, inventory=None, upload_form=UploadForm(),
                plan_form=PlanForm(initial={'artifacts': artifact['digest'], 'action': 'install'}),
                artifact=artifact,
            ))
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
    return render(request, 'admin/skeleton/releases.html', _context(
        request, upload_form=form, plan_form=PlanForm(), inventory=None,
    ), status=400)


@reauthentication_required(allow_get=True)
def preview(request):
    require_platform(request.user)
    if request.method != 'POST':
        return redirect('admin:skeleton_releases')
    _recent_authentication(request)
    form = PlanForm(request.POST)
    if form.is_valid():
        try:
            row = create_preview(user=request.user, **form.cleaned_data)
            return redirect('admin:skeleton_release_operation', operation_id=row.pk)
        except (ValueError, RuntimeError, OSError) as exc:
            _error(request, exc)
    return render(request, 'admin/skeleton/releases.html', _context(
        request, upload_form=UploadForm(), plan_form=form, inventory=None,
    ), status=400)


@reauthentication_required(allow_get=True)
def operation(request, operation_id):
    require_platform(request.user)
    row = get_object_or_404(DeploymentOperation.objects, pk=operation_id)
    if request.method == 'POST':
        _recent_authentication(request)
        form = ConfirmationForm(request.POST)
        if not form.is_valid() or form.cleaned_data['plan_digest'] != row.plan_digest:
            return HttpResponse('Confirmação do plano inválida.', status=400)
        if request.POST.get('intent') == 'retry':
            retry_submission(user=request.user, operation_id=row.pk, revision=form.cleaned_data['revision'])
        else:
            confirm_operation(user=request.user, operation_id=row.pk, revision=form.cleaned_data['revision'])
        return redirect('admin:skeleton_release_operation', operation_id=row.pk)
    status_url = None
    try:
        if row.state == 'preview':
            status_url = agent_client().status_ticket(str(row.pk)).get('status_url')
        else:
            status = agent_client().operation(str(row.pk))
            status_url = status.get('status_url')
            reconcile_operation(row, status)
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
    return render(request, 'admin/skeleton/release_operation.html', _context(
        request, operation=row,
        confirmation_form=ConfirmationForm(initial={'revision': row.revision, 'plan_digest': row.plan_digest}),
        status_url=status_url,
    ))


@reauthentication_required(allow_get=True)
def fetch_release(request):
    require_platform(request.user)
    if request.method != 'POST':
        return redirect('admin:skeleton_releases')
    _recent_authentication(request)
    try:
        identifiers = {name: request.POST.get(name, '') for name in ('component_id', 'version', 'source_id')}
        if any(not value or len(value) > 128 for value in identifiers.values()):
            return HttpResponse('Identificação de release inválida.', status=400)
        artifact = agent_client().fetch_release(**identifiers)
        return render(request, 'admin/skeleton/releases.html', _context(
            request, upload_form=UploadForm(), inventory=None, artifact=artifact,
            plan_form=PlanForm(initial={'action': 'update', 'artifacts': artifact['digest']}),
        ))
    except (ValueError, RuntimeError, OSError) as exc:
        _error(request, exc)
        return redirect('admin:skeleton_releases')


def urlpatterns(site):
    return [
        path('system/releases/', site.admin_view(index), name='skeleton_releases'),
        path('system/releases/upload/', site.admin_view(upload), name='skeleton_release_upload'),
        path('system/releases/preview/', site.admin_view(preview), name='skeleton_release_preview'),
        path('system/releases/fetch/', site.admin_view(fetch_release), name='skeleton_release_fetch'),
        path('system/releases/operations/<uuid:operation_id>/', site.admin_view(operation), name='skeleton_release_operation'),
    ]
