"""Tenant activation is a server-side authorization gate, not a schema installer."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse

from skeleton.common.exceptions import ApplicationUnavailable, ConcurrentUpdate, MissingTenantContext
from skeleton.tenancy.context import current_context

from .app_registry import (
    all_apps,
    dependent_labels,
    get_app,
    required_labels,
    validate_configuration,
    validate_dependencies,
)


def application_state(app_label):
    from .models import AppInstallation

    if current_context().organization_id is None:
        raise MissingTenantContext("Application state requires tenant context.")
    from .availability import app_available

    manifest = get_app(app_label)
    record = AppInstallation.objects.filter(app_label=app_label).first()
    return {
        "app_label": app_label,
        "available": app_available(app_label),
        "label": manifest.label,
        "description": manifest.description,
        "app_id": manifest.app_id,
        "version": manifest.version,
        "enabled": record.enabled if record else manifest.default_enabled,
        "configuration": validate_configuration(manifest, record.configuration if record else {}),
        "revision": record.revision if record else 0,
        "sysid": str(record.pk) if record else None,
    }


def require_application_enabled(app_label):
    try:
        manifest = get_app(app_label)
    except KeyError:
        return  # The core has no optional tenant activation contract.
    state = application_state(manifest.app_label)
    if not state["available"]:
        raise ApplicationUnavailable("Application is unavailable to this organization.")
    if not state["enabled"]:
        raise PermissionDenied("Application is disabled in this organization.")
    # Transitive: a dependency's own disabled or unavailable dependency also closes this application.
    for dependency in required_labels(manifest.app_label):
        state = application_state(dependency)
        if not state["available"]:
            raise ApplicationUnavailable("Application dependency is unavailable.")
        if not state["enabled"]:
            raise PermissionDenied("Application dependency is disabled.")


@transaction.atomic
def configure_application(app_label, *, user, enabled, configuration, revision):
    from skeleton.access.services import require_organization_admin
    from skeleton.tenancy.models import Organization

    from .models import AppInstallation

    require_organization_admin(user)
    manifest = get_app(app_label)
    validate_dependencies()
    if type(enabled) is not bool:
        raise ValidationError({"enabled": "Use true or false."})
    values = validate_configuration(manifest, configuration)
    # Serialize even the first virtual revision=0 transition and dependency changes.
    Organization.objects.select_for_update().get(pk=current_context().organization_id)
    record = AppInstallation.objects.filter(app_label=app_label).first()
    expected = record.revision if record else 0
    if type(revision) is not int or revision != expected:
        raise ConcurrentUpdate("Application configuration changed.")
    from .availability import available_with_dependencies

    if enabled and not available_with_dependencies(app_label):
        # Disabling stays allowed so an organization can close an app the platform withdrew.
        raise ValidationError(
            {"enabled": ValidationError("Application is unavailable to this organization.", code="app_unavailable")}
        )
    if enabled:
        pending = [label for label in required_labels(app_label) if not application_state(label)["enabled"]]
        if pending:
            names = ", ".join(get_app(label).label for label in pending)
            raise ValidationError({"enabled": f"Enable required applications first: {names}."})
    else:
        blocking = [label for label in dependent_labels(app_label) if application_state(label)["enabled"]]
        if blocking:
            names = ", ".join(get_app(label).label for label in blocking)
            raise ValidationError({"enabled": f"Disable dependent applications first: {names}."})
    if record is None:
        record = AppInstallation(
            organization_id=current_context().organization_id,
            app_label=app_label,
            enabled=enabled,
            configuration=values,
        )
        record.save()
    else:
        record.enabled = enabled
        record.configuration = values
        record.save(update_fields={"enabled", "configuration"})
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
            result.append(
                {
                    "app_label": manifest.app_label,
                    "label": entry.label,
                    "url": reverse(entry.url_name, kwargs={"organization_id": current_context().organization_id}),
                }
            )
    return result


def application_navigation(request):
    context = current_context()
    if context.organization_id is None or not getattr(request.user, "is_authenticated", False):
        return {}
    from skeleton.access.policies import is_organization_admin

    return {
        "application_navigation": navigation_for_user(request.user),
        "can_manage_applications": is_organization_admin(request.user),
    }
