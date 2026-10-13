"""Platform control plane. Privileged deployment work belongs to the agent."""
from copy import deepcopy
from uuid import UUID, uuid4

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from skeleton.accounts.mfa import mfa_required
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context
from .models import ComponentInstallation, DeploymentOperation


class DeploymentUnavailable(RuntimeError):
    pass


def require_platform(user):
    if not (user.is_authenticated and user.is_active and user.deleted_at is None
            and user.is_staff and user.is_superuser and not mfa_required(user)):
        raise PermissionDenied('Platform administration required.')
    if current_context().organization_id is not None:
        raise PermissionDenied('Deployment operations are global.')


def agent_client():
    from .deployment_client import AgentClient
    socket = getattr(settings, 'SKELETON_AGENT_SOCKET', '')
    token = getattr(settings, 'SKELETON_AGENT_TOKEN_FILE', '')
    if not socket or not token:
        raise DeploymentUnavailable('Deployment agent is not configured.')
    return AgentClient(socket, token)


def create_preview(*, user, action, artifacts=(), app_ids=()):
    require_platform(user)
    if action not in {'install', 'update', 'uninstall', 'reinstall', 'core_update'}:
        raise ValidationError('Unsupported deployment operation.')
    plan = agent_client().preview(action, artifacts=list(artifacts), app_ids=list(app_ids))
    if plan.get('action') != action or not isinstance(plan.get('generation'), int):
        raise DeploymentUnavailable('Invalid agent preview response.')
    operation = DeploymentOperation(
        operation=action, expected_generation=plan['generation'],
        plan_digest=plan['digest'], plan=plan,
    )
    operation.save()
    return operation


def confirm_operation(*, user, operation_id, revision):
    require_platform(user)
    operation = DeploymentOperation.objects.select_for_update().get(pk=operation_id)
    if operation.revision != revision:
        raise ConcurrentUpdate('Deployment preview changed.')
    if operation.state != 'preview':
        raise ConcurrentUpdate('Deployment preview has already been submitted.')
    inventory = agent_client().inventory()
    if inventory['generation'] != operation.expected_generation:
        raise ConcurrentUpdate('Deployment generation changed; create a new preview.')
    ctx = current_context()
    values = dict(
        plan_id=operation.plan['id'], plan_digest=operation.plan_digest,
        generation=operation.expected_generation, operation_id=str(operation.pk),
        actor_id=str(user.pk), request_id=str(ctx.request_id or uuid4()),
    )
    operation.state = 'approved'
    operation.stage = 'awaiting_agent'
    # Keep approval identity until the journal acknowledges delivery. HTTP retries
    # have their own audit IDs, but must resend this exact logical request.
    operation.result = {'submission': values}
    operation.save(update_fields={'state', 'stage', 'result'})
    _schedule_submission(user=user, values=values)
    return operation


def _schedule_submission(*, user, values):
    values = deepcopy(values)
    request_id = current_context().request_id

    def submit():
        # Values are materialized; never inherit a tenant query after commit.
        try:
            status = agent_client().submit(**values)
            from skeleton.tenancy.context import request_context
            with request_context(actor=user, request_id=request_id, source='deployment:submit'):
                row = DeploymentOperation.objects.select_for_update().get(pk=values['operation_id'])
                reconcile_operation(row, status)
        except Exception as exc:
            from skeleton.tenancy.context import request_context
            with request_context(actor=user, request_id=request_id, source='deployment:submit'):
                row = DeploymentOperation.objects.select_for_update().get(pk=values['operation_id'])
                if row.state == 'approved':
                    row.state = 'submission_failed'
                    row.stage = 'awaiting_agent'
                    row.error_code = type(exc).__name__[:100]
                    row.save(update_fields={'state', 'stage', 'error_code'})

    transaction.on_commit(submit)


def retry_submission(*, user, operation_id, revision):
    """Explicit retry of delivery only; the agent UUID remains idempotent."""
    require_platform(user)
    row = DeploymentOperation.objects.select_for_update().get(pk=operation_id)
    if row.revision != revision or row.state != 'submission_failed':
        raise ConcurrentUpdate('Deployment submission changed.')
    values = row.result.get('submission') if isinstance(row.result, dict) else None
    expected = {
        'plan_id': row.plan['id'], 'plan_digest': row.plan_digest,
        'generation': row.expected_generation, 'operation_id': str(row.pk),
    }
    if (not isinstance(values, dict) or set(values) != {*expected, 'actor_id', 'request_id'}
            or any(values.get(name) != value for name, value in expected.items())):
        raise DeploymentUnavailable('Original approval is unavailable; reconcile the agent journal.')
    try:
        UUID(values['actor_id'])
        UUID(values['request_id'])
    except (ValueError, TypeError, AttributeError):
        raise DeploymentUnavailable('Invalid original approval identity.') from None
    # Delivery is not a fresh approval: the agent checks the original generation
    # for a new operation and returns an existing UUID before checking live state.
    row.state = 'approved'
    row.stage = 'awaiting_agent'
    row.error_code = ''
    row.save(update_fields={'state', 'stage', 'error_code'})
    _schedule_submission(user=user, values=values)
    return row


def reconcile_operation(row, status):
    try:
        operation_id = UUID(str(status['id']))
        state = status['state']
        stage = status['stage']
        if (state not in {'queued', 'running', 'succeeded', 'failed', 'interrupted', 'restored', 'cancelled'}
                or not isinstance(stage, str) or not 0 < len(stage) <= 64
                or type(status['generation']) is not int or status['generation'] < 0
                or type(status['expected_generation']) is not int):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DeploymentUnavailable('Invalid agent operation response.') from None
    if (operation_id != row.pk
            or status['expected_generation'] != row.expected_generation
            or status.get('plan_digest') != row.plan_digest
            or status.get('operation') != row.operation
            or status.get('action') != row.operation
            or 'plan' in status and status['plan'] != row.plan):
        raise DeploymentUnavailable('Agent operation approval mismatch.')
    result = deepcopy(status)
    # Status tickets are short-lived credentials, not durable operation evidence.
    result.pop('status_url', None)
    changes = set()
    values = {'state': status['state'], 'stage': status.get('stage', status['state']),
              'error_code': str(status.get('error') or '')[:100], 'result': result}
    for name, value in values.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changes.add(name)
    if changes:
        row.save(update_fields=changes)
    return row


def synchronize_inventory(inventory, *, using='default'):
    generation = inventory['generation']
    components = []
    core = inventory.get('core')
    if core:
        components.append({**core, 'component_id': core.get('component_id', 'django-skeleton'), 'kind': 'core'})
    for entry in inventory.get('apps', []):
        manifest = entry.get('manifest', entry)
        components.append({**manifest, **{key: entry[key] for key in ('state', 'digest', 'activation_policy') if key in entry},
                           'component_id': manifest.get('app_id', manifest.get('component_id')), 'kind': 'app'})
    for data in components:
        identifier = data['component_id']
        if not identifier:
            raise DeploymentUnavailable('Agent component has no stable identity.')
        row = ComponentInstallation.objects.using(using).filter(component_id=identifier).first()
        values = {
            'kind': data['kind'], 'app_label': data.get('app_label', ''),
            'package_name': data.get('package_name', ''), 'version': data['version'],
            'digest': data.get('digest', ''), 'publisher': data.get('publisher', ''),
            'generation': generation, 'state': data.get('state', 'installed'),
            'inventory_reference': str(data.get('inventory_reference', data.get('digest', '')))[:200],
        }
        if row is None:
            ComponentInstallation(component_id=identifier, **values).save(using=using)
        else:
            changes = {name for name, value in values.items() if getattr(row, name) != value}
            for name in changes:
                setattr(row, name, values[name])
            if changes:
                row.save(using=using, update_fields=changes)
    return components
