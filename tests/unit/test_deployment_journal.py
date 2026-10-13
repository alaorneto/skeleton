"""Deterministic delivery and lock contracts, without executing privileged commands."""
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread, current_thread
from types import SimpleNamespace
from uuid import uuid4

import pytest

from deployment_agent import service as service_module
from deployment_agent import storage
from deployment_agent.server import ControlHandler, UnixHTTPServer
from deployment_agent.service import DeploymentService
from deployment_agent.storage import AgentError, Store, atomic_json, deployment_lock, digest
from deployment_agent.releases import owned_snapshot
from skeleton.system.deployment_client import AgentClient, AgentError as ClientError


@pytest.fixture
def approved_deployment(tmp_path, monkeypatch):
    source, release = tmp_path / 'source', tmp_path / 'release'
    source.mkdir()
    release.mkdir()
    (source / 'approved.txt').write_text('before')
    (release / 'approved.txt').write_text('after')
    source_owned, target_owned = owned_snapshot(source), owned_snapshot(release)
    store = Store(tmp_path / 'journal')
    plan = {
        'id': str(uuid4()), 'generation': 0, 'action': 'install',
        'release': str(release), 'artifacts': [], 'migrations': [],
        'applied_migrations': [], 'migration_targets': [],
    }
    plan['digest'] = digest(plan)
    previous_inventory = {'generation': 0}
    target_inventory = {'generation': 1}
    store.put('plans', plan['id'], {
        'plan': plan, 'previous_release': str(source),
        'source_owned_digest': digest(source_owned), 'owned_files': target_owned,
        'previous_owned_files': source_owned, 'previous_inventory': previous_inventory,
        'inventory': target_inventory,
    })
    atomic_json(store.root / 'active.json', {
        'release': str(source), 'inventory': previous_inventory, 'owned_files': source_owned,
    })
    service = DeploymentService.__new__(DeploymentService)
    service.store = store
    service.worker = None
    service.config = {
        'maintenance_marker': str(tmp_path / 'maintenance'),
        'releases_root': str(tmp_path), 'minimum_free_bytes': 0,
    }
    service.backend = SimpleNamespace(
        active=lambda: source,
        probe=lambda path: {'migrations': [], 'applied': [], 'targets': []},
        drain=lambda *args: None,
        backup=lambda *args: str(tmp_path / 'backup.json'),
    )
    pending = []
    class DeferredWorker:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            pending.append(self)
    monkeypatch.setattr(service_module, 'threading', SimpleNamespace(Thread=DeferredWorker))
    values = {
        'plan_id': plan['id'], 'plan_digest': plan['digest'], 'generation': 0,
        'operation_id': str(uuid4()), 'actor_id': str(uuid4()), 'request_id': str(uuid4()),
    }
    return service, values, pending


def test_duplicate_delivery_reads_journal_while_execution_lock_is_held(approved_deployment):
    service, values, pending = approved_deployment
    service.submit(**values)
    operation = service.store.get('operations', values['operation_id'])
    with deployment_lock(service.store.root):
        service.stage(operation, 'backup_ready', state='running')
        result = service.submit(**values)
        assert result['state'] == 'running'
        assert result['stage'] == 'backup_ready'
        changed = {**values, 'request_id': str(uuid4())}
        with pytest.raises(AgentError, match='idempotency_conflict'):
            service.submit(**changed)
    assert len(pending) == 1
    assert service.store.get('operations', values['operation_id'])['binding']['request_id'] == values['request_id']


def test_duplicate_delivery_after_generation_advance_does_not_restart_execution(approved_deployment):
    service, values, pending = approved_deployment
    service.submit(**values)
    operation = service.store.get('operations', values['operation_id'])
    service.stage(operation, 'ready', state='succeeded', generation=1)
    active = service.state()
    active['inventory']['generation'] = 1
    atomic_json(service.store.root / 'active.json', active)
    assert service.submit(**values)['state'] == 'succeeded'
    assert len(pending) == 1
    with pytest.raises(AgentError, match='idempotency_conflict'):
        service.submit(**{**values, 'actor_id': str(uuid4())})
    with pytest.raises(AgentError, match='stale_approval'):
        service.submit(**{**values, 'operation_id': str(uuid4())})
    assert len(pending) == 1


def test_queued_execution_waits_for_lock_and_completed_journal_cannot_execute_again(approved_deployment, monkeypatch):
    service, values, _ = approved_deployment
    service.submit(**values)
    lock_attempted, completed = Event(), Event()
    errors, finishes = [], []
    original_flock = storage.fcntl.flock
    def observed_flock(descriptor, mode):
        if current_thread() is worker and mode & storage.fcntl.LOCK_EX:
            lock_attempted.set()
        return original_flock(descriptor, mode)
    monkeypatch.setattr(storage.fcntl, 'flock', observed_flock)
    def finish(operation, record):
        finishes.append(operation['id'])
        service.stage(operation, 'ready', state='succeeded', generation=1)
    monkeypatch.setattr(service, 'finish', finish)
    def execute():
        try:
            service.execute(values['operation_id'])
        except Exception as error:
            errors.append(error)
        finally:
            completed.set()
    worker = Thread(target=execute)
    try:
        with deployment_lock(service.store.root):
            worker.start()
            assert lock_attempted.wait(5), 'Worker did not attempt the deployment lock.'
            assert not completed.is_set()
            assert service.store.get('operations', values['operation_id'])['state'] == 'queued'
    finally:
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert finishes == [values['operation_id']]
    journal = deepcopy(service.store.get('operations', values['operation_id']))
    assert journal['state'] == 'succeeded'
    service.execute(values['operation_id'])
    assert finishes == [values['operation_id']]
    assert service.store.get('operations', values['operation_id']) == journal


def test_failed_execution_journals_error_before_releasing_lock(approved_deployment, monkeypatch):
    service, values, _ = approved_deployment
    service.submit(**values)
    def fail_probe(path):
        raise AgentError('migration_plan_changed', 409)
    monkeypatch.setattr(service.backend, 'probe', fail_probe)
    service.execute(values['operation_id'])
    operation = service.store.get('operations', values['operation_id'])
    assert operation['state'] == 'failed'
    assert operation['stage'] == 'failed'
    assert operation['error'] == 'migration_plan_changed'
    assert not Path(service.config['maintenance_marker']).exists()
    journal = deepcopy(operation)
    assert service.submit(**values)['error'] == 'migration_plan_changed'
    service.execute(values['operation_id'])
    assert service.store.get('operations', values['operation_id']) == journal


def test_real_socket_lost_ack_resends_exact_binding_once(approved_deployment, tmp_path):
    service, values, pending = approved_deployment
    token_file = tmp_path / 'control-token'
    token_file.write_text('disposable-control-token-at-least-32-chars')
    token_file.chmod(0o600)
    service.config['token_file'] = str(token_file)
    class LostAckHandler(ControlHandler):
        def send_json(self, value, status=200):
            if self.path == '/submit' and self.server.drop_ack:
                self.server.drop_ack = False
                return  # The journal was committed; disconnect before the HTTP ACK.
            return super().send_json(value, status)
    socket_path = str(tmp_path / 'a.sock')
    with UnixHTTPServer(socket_path, LostAckHandler) as server:
        server.service = service
        server.tickets = SimpleNamespace(issue=lambda operation_id: None)
        server.drop_ack = True
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        client = AgentClient(socket_path, token_file, timeout=5)
        try:
            with pytest.raises(ClientError, match='agent_unavailable'):
                client.submit(**values)
            committed = deepcopy(service.store.get('operations', values['operation_id']))
            assert committed['binding']['request_id'] == values['request_id']
            assert client.submit(**values)['state'] == 'queued'
            assert len(service.store.operations()) == 1
            assert len(pending) == 1
            assert service.store.get('operations', values['operation_id']) == committed
            with pytest.raises(ClientError, match='idempotency_conflict'):
                client.submit(**{**values, 'request_id': str(uuid4())})
            assert len(pending) == 1
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize('action', ['abort', 'restore', 'complete'])
def test_recovery_of_superseded_operation_cannot_touch_new_release(approved_deployment, monkeypatch, action):
    service, values, _ = approved_deployment
    service.submit(**values)
    operation = service.store.get('operations', values['operation_id'])
    service.stage(operation, 'failed', state='failed', error='preflight_failed')
    active = service.state()
    active['release'] = str(service.store.root.parent / 'newer-release')
    active['inventory']['generation'] = 1
    atomic_json(service.store.root / 'active.json', active)
    monkeypatch.setattr(service.backend, 'drain', lambda *args: pytest.fail('Superseded recovery drained traffic.'))
    journal = deepcopy(service.store.get('operations', values['operation_id']))
    with pytest.raises(AgentError, match='operation_superseded'):
        service.recover(values['operation_id'], action, acknowledge_new_writes=True)
    assert service.state() == active
    assert service.store.get('operations', values['operation_id']) == journal
    assert not Path(service.config['maintenance_marker']).exists()


def test_recovery_cannot_interfere_with_another_queued_operation(approved_deployment, monkeypatch):
    service, values, pending = approved_deployment
    service.submit(**values)
    operation = service.store.get('operations', values['operation_id'])
    service.stage(operation, 'failed', state='failed', error='preflight_failed')
    record = deepcopy(service.store.get('plans', values['plan_id']))
    plan = record['plan']
    plan['id'] = str(uuid4())
    plan['digest'] = digest({key: value for key, value in plan.items() if key != 'digest'})
    service.store.put('plans', plan['id'], record)
    other = {**values, 'operation_id': str(uuid4()), 'plan_id': plan['id'], 'plan_digest': plan['digest']}
    service.submit(**other)
    monkeypatch.setattr(service.backend, 'drain', lambda *args: pytest.fail('Recovery drained another operation.'))
    journal = deepcopy(service.store.operations())
    with pytest.raises(AgentError, match='recovery_or_operation_pending'):
        service.recover(values['operation_id'], 'abort')
    assert service.store.operations() == journal
    assert len(pending) == 2
    assert not Path(service.config['maintenance_marker']).exists()


def test_current_release_abort_is_explicit_and_cannot_be_replayed(approved_deployment, monkeypatch):
    service, values, pending = approved_deployment
    service.submit(**values)
    operation = service.store.get('operations', values['operation_id'])
    service.stage(operation, 'failed', state='failed', error='preflight_failed')
    calls = []
    monkeypatch.setattr(service.backend, 'manage', lambda *args, **kwargs: calls.append('check'), raising=False)
    monkeypatch.setattr(service.backend, 'cutover', lambda *args: calls.append('cutover'), raising=False)
    monkeypatch.setattr(service.backend, 'start', lambda *args: calls.append('start'), raising=False)
    monkeypatch.setattr(service, 'health', lambda *args: calls.append('health'))
    result = service.recover(values['operation_id'], 'abort')
    assert result['state'] == 'cancelled'
    assert result['stage'] == 'aborted'
    assert service.state()['inventory']['generation'] == 0
    assert not Path(service.config['maintenance_marker']).exists()
    first_calls = calls.copy()
    journal = deepcopy(service.store.get('operations', values['operation_id']))
    with pytest.raises(AgentError, match='operation_not_recoverable'):
        service.recover(values['operation_id'], 'abort')
    service.execute(values['operation_id'])
    assert calls == first_calls
    assert len(pending) == 1
    assert service.store.get('operations', values['operation_id']) == journal
