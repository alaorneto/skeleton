"""Tenant activation is a server-side authorization gate, not a schema installer."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse

from skeleton.common.exceptions import ConcurrentUpdate, MissingTenantContext
from skeleton.tenancy.context import current_context
from .app_registry import all_apps, get_app, validate_configuration, validate_dependencies


def application_state(app_label):
    from .models import AppInstallation
    if current_context().organization_id is None:
        raise MissingTenantContext('Application state requires tenant context.')
    manifest = get_app(app_label)
    record = AppInstallation.objects.filter(app_label=app_label).first()
    return {'app_label': app_label, 'label': manifest.label, 'description': manifest.description,
            'app_id': manifest.app_id, 'version': manifest.version,
            'enabled': record.enabled if record else manifest.default_enabled,
            'configuration': validate_configuration(manifest, record.configuration if record else {}),
            'revision': record.revision if record else 0,
            'sysid': str(record.pk) if record else None}


def require_application_enabled(app_label):
    try:
        manifest = get_app(app_label)
    except KeyError:
        return  # The core has no optional tenant activation contract.
    if not application_state(manifest.app_label)['enabled']:
        raise PermissionDenied('Application is disabled in this organization.')
    for dependency in manifest.dependencies:
        if not application_state(dependency)['enabled']:
            raise PermissionDenied('Application dependency is disabled.')


@transaction.atomic
def configure_application(app_label, *, user, enabled, configuration, revision):
    from skeleton.access.services import require_organization_admin
    from skeleton.tenancy.models import Organization
    from .models import AppInstallation
    require_organization_admin(user)
    manifest = get_app(app_label)
    validate_dependencies()
    if type(enabled) is not bool:
        raise ValidationError({'enabled': 'Use true or false.'})
    values = validate_configuration(manifest, configuration)
    # Serialize even the first virtual revision=0 transition and dependency changes.
    Organization.objects.select_for_update().get(pk=current_context().organization_id)
    record = AppInstallation.objects.filter(app_label=app_label).first()
    expected = record.revision if record else 0
    if type(revision) is not int or revision != expected:
        raise ConcurrentUpdate('Application configuration changed.')
    if enabled:
        for dependency in manifest.dependencies:
            if not application_state(dependency)['enabled']:
                raise ValidationError({'enabled': 'Enable required applications first.'})
    else:
        for other in all_apps():
            if app_label in other.dependencies and application_state(other.app_label)['enabled']:
                raise ValidationError({'enabled': 'Disable dependent applications first.'})
    if record is None:
        record = AppInstallation(organization_id=current_context().organization_id, app_label=app_label,
                                 enabled=enabled, configuration=values)
        record.save()
    else:
        record.enabled = enabled
        record.configuration = values
        record.save(update_fields={'enabled', 'configuration'})
    return record


def navigation_for_user(user):
    from skeleton.access.policies import authorized_queryset
    from skeleton.system.registry import get_spec
    result = []
    for manifest in all_apps():
        try:
            require_application_enabled(manifest.app_label)
        except PermissionDenied:
            continue
        for entry in manifest.navigation:
            try:
                spec = get_spec(entry.table_key)
                authorized_queryset(spec.model, user=user, action=entry.action)
            except PermissionDenied:
                continue
            result.append({'app_label': manifest.app_label, 'label': entry.label,
                           'url': reverse(entry.url_name, kwargs={'organization_id': current_context().organization_id})})
    return result


def application_navigation(request):
    context = current_context()
    if context.organization_id is None or not getattr(request.user, 'is_authenticated', False):
        return {}
    from skeleton.access.policies import is_organization_admin
    return {'application_navigation': navigation_for_user(request.user),
            'can_manage_applications': is_organization_admin(request.user)}
