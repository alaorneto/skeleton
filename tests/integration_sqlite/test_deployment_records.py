import pytest
from django.test import Client

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import ConcurrentUpdate, ImmutableRecord
from skeleton.system.models import ComponentInstallation, DeploymentOperation
from skeleton.tenancy.context import request_context, system_context

pytestmark = pytest.mark.usefixtures('skeleton_db')
PASSWORD = 'Deployment-test-password!49'


def actor(*, platform=False):
    user, _ = bootstrap_identity(email='release-actor@example.test', organization='releases', name='Releases', password=PASSWORD)
    if not platform:
        with system_context(reason='test:platform'):
            user.is_staff = False
            user.is_superuser = False
            user.save(update_fields={'is_staff', 'is_superuser'})
    return user


def test_deployment_plan_and_result_are_redacted_before_audit_persistence():
    from skeleton.audit.models import AuditEvent
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row = DeploymentOperation(operation='install', expected_generation=0, plan_digest='a' * 64,
                                  plan={'private_path': '/private/release', 'credential': 'never-audit'})
        row.save()
        row.result = {'secret_status_ticket': 'not-audit-either'}
        row.state = 'approved'
        row.save(update_fields={'result', 'state'})
        events = list(AuditEvent.objects.filter(record_sysid=row.pk).order_by('record_revision'))
        assert events[0].actor_sysid == user.pk
        assert events[0].changes['plan']['new'] == '[REDACTED]'
        assert events[1].changes['result']['new'] == '[REDACTED]'
        assert events[1].changes['state']['new'] == 'approved'


def test_deployment_plan_is_immutable_and_concurrent_approval_has_one_winner():
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row = DeploymentOperation(operation='update', expected_generation=2, plan_digest='b' * 64, plan={'generation': 2})
        row.save()
        stale = DeploymentOperation.objects.get(pk=row.pk)
        row.state = 'approved'
        row.save(update_fields={'state'})
        stale.state = 'approved'
        with pytest.raises(ConcurrentUpdate):
            stale.save(update_fields={'state'})
        row.plan = {'generation': 99}
        with pytest.raises(ImmutableRecord):
            row.save(update_fields={'plan'})
        assert DeploymentOperation.objects.get(pk=row.pk).plan == {'generation': 2}


def test_component_identity_cannot_be_reassigned_to_another_app():
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row = ComponentInstallation(component_id='tasks', kind='app', app_label='project_tasks',
                                    package_name='tasks', version='1.0.0', digest='c' * 64,
                                    publisher='trusted', generation=1, state='installed')
        row.save()
        row.app_label = 'sk_accounts'
        with pytest.raises(ImmutableRecord):
            row.save(update_fields={'app_label'})
        assert ComponentInstallation.objects.get(pk=row.pk).app_label == 'project_tasks'


def test_organization_admin_cannot_use_release_admin():
    user = actor()
    client = Client()
    assert client.post('/accounts/login/', {'login': user.email, 'password': PASSWORD}).status_code == 302
    assert client.get('/admin/system/releases/').status_code == 403
    assert client.post('/admin/system/releases/preview/', {'action': 'uninstall', 'app_ids': 'tasks'}).status_code == 403


def test_retained_app_can_only_disable_existing_configuration():
    from django.core.exceptions import ValidationError
    from skeleton.system.app_registry import get_app, register_app, unregister_app
    from skeleton.system.models import AppInstallation
    user, organization = bootstrap_identity(email='retained@example.test', organization='retained', name='Retained', password=PASSWORD)
    manifest = get_app('example_notes')
    with system_context(reason='test:retention'):
        ComponentInstallation(component_id='example-notes', kind='app', app_label='example_notes',
                              package_name='notes', version='0.1.0', state='schema_retained').save()
    with system_context(reason='test:retention', organization_id=organization.pk):
        row = AppInstallation(app_label='example_notes', organization=organization, enabled=True, configuration={'heading': 'Preserved'})
        row.save()
    unregister_app('example_notes')
    try:
        with system_context(reason='test:retention', organization_id=organization.pk):
            AppInstallation.objects.filter(pk=row.pk).update(enabled=False)
            row = AppInstallation.objects.get(pk=row.pk)
            assert row.configuration == {'heading': 'Preserved'}
            assert row.enabled is False
            row.enabled = True
            with pytest.raises(ValidationError):
                row.save(update_fields={'enabled'})
            row = AppInstallation.objects.get(pk=row.pk)
            row.enabled = False
            row.configuration = {'heading': 'Changed'}
            with pytest.raises(ValidationError):
                row.save(update_fields={'configuration'})
    finally:
        register_app(manifest)


def test_private_maintenance_smoke_does_not_bypass_authorization_or_writes(tmp_path):
    from django.test import override_settings
    marker, token_file = tmp_path / 'maintenance', tmp_path / 'smoke-token'
    token = 'maintenance-smoke-private-token-32-chars'
    token_file.write_text(token)
    user = actor()
    client = Client()
    assert client.post('/accounts/login/', {'login': user.email, 'password': PASSWORD}).status_code == 302
    marker.touch()
    with override_settings(
        SKELETON_MAINTENANCE_MARKER=str(marker),
        SKELETON_DEPLOYMENT_SMOKE_TOKEN_FILE=str(token_file),
        SKELETON_DEPLOYMENT_SMOKE_PATHS=('/accounts/login/', '/admin/system/releases/'),
    ):
        client = Client()
        assert client.get('/accounts/login/').status_code == 503
        assert client.get('/accounts/login/', HTTP_X_SKELETON_DEPLOYMENT_SMOKE='wrong').status_code == 503
        assert client.get('/accounts/login/', HTTP_X_SKELETON_DEPLOYMENT_SMOKE=token).status_code == 200
        assert client.post('/accounts/login/', {}, HTTP_X_SKELETON_DEPLOYMENT_SMOKE=token).status_code == 503
        assert client.get('/profile/', HTTP_X_SKELETON_DEPLOYMENT_SMOKE=token).status_code == 503
        marker.unlink()
        assert client.post('/accounts/login/', {'login': user.email, 'password': PASSWORD}).status_code == 302
        marker.touch()
        assert client.get('/admin/system/releases/', HTTP_X_SKELETON_DEPLOYMENT_SMOKE=token).status_code == 403


def test_release_upload_is_bounded_and_still_requires_csrf(tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import override_settings
    user = actor(platform=True)
    client = Client(enforce_csrf_checks=True)
    client.get('/accounts/login/')
    csrf = client.cookies['csrftoken'].value
    assert client.post('/accounts/login/', {'login': user.email, 'password': PASSWORD, 'csrfmiddlewaretoken': csrf}).status_code == 302
    csrf = client.cookies['csrftoken'].value
    assert client.post('/admin/system/releases/upload/', {'package': SimpleUploadedFile('app.skapp', b'unsigned')}).status_code == 403
    with override_settings(SKELETON_PACKAGE_MAX_BYTES=8):
        response = client.post('/admin/system/releases/upload/', {
            'csrfmiddlewaretoken': csrf, 'package': SimpleUploadedFile('app.skapp', b'123456789'),
        })
        assert response.status_code == 413
    assert DeploymentOperation.objects.count() == 0


def test_status_ticket_rotation_does_not_rewrite_durable_state():
    from skeleton.audit.models import AuditEvent
    from skeleton.system.releases import reconcile_operation
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row = DeploymentOperation(operation='install', expected_generation=0, plan_digest='d' * 64, plan={'generation': 0})
        row.save()
        status = {'id': str(row.pk), 'state': 'running', 'stage': 'backup_ready',
                  'status_url': 'https://status.example.test/?ticket=private-first'}
        reconcile_operation(row, status)
        revision = row.revision
        event_ids = set(AuditEvent.objects.filter(record_sysid=row.pk).values_list('pk', flat=True))
        status['status_url'] = 'https://status.example.test/?ticket=private-second'
        reconcile_operation(row, status)
        persisted = DeploymentOperation.objects.get(pk=row.pk)
        assert persisted.revision == revision
        assert 'status_url' not in persisted.result
        assert set(AuditEvent.objects.filter(record_sysid=row.pk).values_list('pk', flat=True)) == event_ids


def test_failed_operation_refresh_shows_offline_recovery(monkeypatch):
    from types import SimpleNamespace
    from skeleton.system import release_admin
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row = DeploymentOperation(operation='update', state='failed', stage='failed',
                                  expected_generation=0, plan_digest='e' * 64, plan={'generation': 0},
                                  error_code='migration_failed')
        row.save()
    status = {'id': str(row.pk), 'state': 'restored', 'stage': 'restored',
              'error': None, 'status_url': 'https://status.example.test/?ticket=private-recovery'}
    monkeypatch.setattr(release_admin, 'agent_client',
                        lambda: SimpleNamespace(operation=lambda operation_id: status))
    client = Client()
    assert client.post('/accounts/login/', {'login': user.email, 'password': PASSWORD}).status_code == 302
    assert client.get(f'/admin/system/releases/operations/{row.pk}/').status_code == 200
    row.refresh_from_db()
    assert row.state == 'restored'
    assert row.stage == 'restored'
    assert row.error_code == ''
    assert 'status_url' not in row.result
