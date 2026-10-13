import pytest
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4
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


def agent_status(row, *, state='running', stage='backup_ready', **values):
    return {
        'id': str(row.pk), 'operation': row.operation, 'action': row.operation,
        'expected_generation': row.expected_generation, 'generation': row.expected_generation,
        'plan_digest': row.plan_digest, 'state': state, 'stage': stage, **values,
    }


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
        status = agent_status(row, status_url='https://status.example.test/?ticket=private-first')
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
    from skeleton.system import release_admin
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row = DeploymentOperation(operation='update', state='failed', stage='failed',
                                  expected_generation=0, plan_digest='e' * 64, plan={'generation': 0},
                                  error_code='migration_failed')
        row.save()
    status = agent_status(row, state='restored', stage='restored', error=None,
                          status_url='https://status.example.test/?ticket=private-recovery')
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


@pytest.mark.parametrize('accepted', [False, True])
def test_lost_submission_ack_retries_original_approval_without_new_execution(monkeypatch, accepted):
    from skeleton.audit.models import AuditEvent
    from skeleton.system import releases
    from skeleton.system.deployment_client import AgentError
    user = actor(platform=True)
    approval_id, retry_id = uuid4(), uuid4()
    attempts, bindings, executions = [], {}, []
    with request_context(actor=user, request_id=approval_id, source='test:deployment'):
        row = DeploymentOperation(operation='install', expected_generation=0,
                                  plan_digest='f' * 64, plan={'id': str(uuid4()), 'generation': 0})
        row.save()

    def submit(**values):
        attempts.append(deepcopy(values))
        if len(attempts) == 1 and not accepted:
            raise AgentError('agent_unavailable')
        binding = {key: value for key, value in values.items() if key != 'operation_id'}
        existing = bindings.get(values['operation_id'])
        if existing is not None and existing != binding:
            raise AgentError('idempotency_conflict', 409)
        if existing is None:
            bindings[values['operation_id']] = binding
            executions.append(values['operation_id'])
        if len(attempts) == 1:
            raise AgentError('agent_unavailable')  # Accepted, but the ACK never arrived.
        return agent_status(row, state='succeeded', stage='ready', generation=1)

    client = SimpleNamespace(inventory=lambda: {'generation': 0}, submit=submit)
    monkeypatch.setattr(releases, 'agent_client', lambda: client)
    with request_context(actor=user, request_id=approval_id, source='test:deployment'):
        releases.confirm_operation(user=user, operation_id=row.pk, revision=row.revision)
        assert attempts == []  # Submission must wait for COMMIT.
    with request_context(actor=user, request_id=retry_id, source='test:deployment'):
        row.refresh_from_db()
        assert row.state == 'submission_failed'
        assert row.result['submission'] == attempts[0]
        failed_revision = row.revision
        # A completed operation can already have advanced inventory. Delivery
        # retry must neither ask for it nor create a different approval.
        client.inventory = lambda: pytest.fail('Retry queried live generation.')
        releases.retry_submission(user=user, operation_id=row.pk, revision=failed_revision)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row.refresh_from_db()
        assert attempts[1] == attempts[0]
        assert attempts[1]['request_id'] == str(approval_id)
        assert len(executions) == 1
        assert row.state == 'succeeded'
        assert row.result['generation'] == 1
        assert 'submission' not in row.result
        assert row.expected_generation == 0
        retry_events = AuditEvent.objects.filter(record_sysid=row.pk, record_revision__gt=failed_revision)
        assert {event.request_id for event in retry_events} == {retry_id}
        assert all(event.changes.get('result', {}).get('new') in (None, '[REDACTED]')
                   for event in retry_events)
        with pytest.raises(ConcurrentUpdate):
            releases.retry_submission(user=user, operation_id=row.pk, revision=failed_revision)
        assert len(attempts) == 2


def test_submission_retry_preserves_approver_when_another_platform_user_delivers(monkeypatch):
    from skeleton.system import releases
    from skeleton.system.deployment_client import AgentError
    user = actor(platform=True)
    other, _ = bootstrap_identity(email='other-release@example.test', organization='other-release',
                                 name='Other release', password=PASSWORD)
    with system_context(reason='test:platform'):
        other.is_staff = True
        other.is_superuser = True
        other.save(update_fields={'is_staff', 'is_superuser'})
    attempts = []
    with request_context(actor=user, request_id=uuid4(), source='test:deployment'):
        row = DeploymentOperation(operation='update', expected_generation=4,
                                  plan_digest='a' * 64, plan={'id': str(uuid4()), 'generation': 4})
        row.save()
    def submit(**values):
        attempts.append(values)
        if len(attempts) == 1:
            raise AgentError('agent_unavailable')
        return agent_status(row, state='queued', stage='approved')
    monkeypatch.setattr(releases, 'agent_client',
                        lambda: SimpleNamespace(inventory=lambda: {'generation': 4}, submit=submit))
    with request_context(actor=user, request_id=uuid4(), source='test:deployment'):
        releases.confirm_operation(user=user, operation_id=row.pk, revision=row.revision)
    with request_context(actor=other, request_id=uuid4(), source='test:deployment'):
        row.refresh_from_db()
        releases.retry_submission(user=other, operation_id=row.pk, revision=row.revision)
    with request_context(actor=other, request_id=None, source='test:deployment'):
        row.refresh_from_db()
        assert attempts[1] == attempts[0]
        assert attempts[1]['actor_id'] == str(user.pk)
        assert row.updated_by == other.pk
        assert row.state == 'queued'


def test_submission_retry_without_durable_binding_fails_closed(monkeypatch):
    from skeleton.system import releases
    user = actor(platform=True)
    monkeypatch.setattr(releases, 'agent_client', lambda: pytest.fail('Missing approval was sent.'))
    with request_context(actor=user, request_id=uuid4(), source='test:deployment'):
        row = DeploymentOperation(operation='install', state='submission_failed',
                                  expected_generation=0, plan_digest='a' * 64,
                                  plan={'id': str(uuid4()), 'generation': 0})
        row.save()
        with pytest.raises(releases.DeploymentUnavailable, match='Original approval'):
            releases.retry_submission(user=user, operation_id=row.pk, revision=row.revision)
        persisted = DeploymentOperation.objects.get(pk=row.pk)
        assert persisted.state == 'submission_failed'
        assert persisted.revision == row.revision


@pytest.mark.parametrize('changes', [
    {'id': str(uuid4())}, {'state': 'preview'}, {'stage': ''},
    {'generation': True}, {'expected_generation': True},
    {'expected_generation': 7}, {'plan_digest': 'b' * 64},
    {'action': 'uninstall'}, {'operation': 'uninstall'}, {'plan': {'generation': 99}},
])
def test_invalid_status_cannot_overwrite_approved_operation(changes):
    from skeleton.system.releases import DeploymentUnavailable, reconcile_operation
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row = DeploymentOperation(operation='install', state='approved', expected_generation=0,
                                  plan_digest='a' * 64, plan={'generation': 0},
                                  result={'submission': {'request_id': str(uuid4())}})
        row.save()
        original_result = deepcopy(row.result)
        revision = row.revision
        with pytest.raises(DeploymentUnavailable):
            reconcile_operation(row, agent_status(row, **changes))
        persisted = DeploymentOperation.objects.get(pk=row.pk)
        assert persisted.revision == revision
        assert persisted.result == original_result
        assert persisted.state == 'approved'


def test_stale_release_confirmation_returns_conflict_without_delivery(monkeypatch):
    from skeleton.system import releases
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row = DeploymentOperation(operation='install', expected_generation=0,
                                  plan_digest='a' * 64, plan={'id': str(uuid4()), 'generation': 0})
        row.save()
    monkeypatch.setattr(releases, 'agent_client', lambda: pytest.fail('Stale approval contacted agent.'))
    client = Client()
    assert client.post('/accounts/login/', {'login': user.email, 'password': PASSWORD}).status_code == 302
    response = client.post(f'/admin/system/releases/operations/{row.pk}/', {
        'revision': row.revision + 1, 'plan_digest': row.plan_digest,
    })
    assert response.status_code == 409
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row.refresh_from_db()
        assert row.state == 'preview'


def test_invalid_journal_rolls_back_inventory_and_preserves_operation(tmp_path):
    import json
    from django.core.management import call_command
    from django.core.management.base import CommandError
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        row = DeploymentOperation(operation='install', state='approved', expected_generation=0,
                                  plan_digest='a' * 64, plan={'generation': 0})
        row.save()
    inventory_path, journal_path = tmp_path / 'inventory.json', tmp_path / 'operation.json'
    inventory_path.write_text(json.dumps({
        'generation': 1, 'apps': [{
            'app_id': 'pending-component', 'app_label': 'pending_component',
            'package_name': 'pending_component', 'version': '1.0.0', 'state': 'installed',
        }],
    }))
    journal_path.write_text(json.dumps(agent_status(row, state='preview', stage='preview')))
    with pytest.raises(CommandError, match='DeploymentUnavailable'):
        call_command('sync_deployment_state', inventory=inventory_path, operation=journal_path)
    with request_context(actor=user, request_id=None, source='test:deployment'):
        persisted = DeploymentOperation.objects.get(pk=row.pk)
        assert persisted.state == 'approved'
        assert persisted.revision == row.revision
        assert not ComponentInstallation.objects.filter(component_id='pending-component').exists()
