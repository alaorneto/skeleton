"""Tenant activation is a server-side authorization gate, not a schema installer."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext

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

_MEMO = "skeleton.system.application_states"
_STALE = object()  # A write happened in this request: stop memoizing (a savepoint may roll it back).


def _memo_key():
    context = current_context()
    return (_MEMO, context.using, context.organization_id)


def forget_application_states(*, everywhere=False):
    """Stop memoizing states in this request after a write (activation here, availability anywhere)."""
    from skeleton.tenancy.context import request_cache

    cache = request_cache()
    if cache is None:
        return
    if everywhere:
        cache[(_MEMO, "global")] = _STALE
    cache[_memo_key()] = _STALE


def _load_states():
    from .availability import availability_map
    from .models import AppInstallation

    manifests = all_apps()
    labels = [manifest.app_label for manifest in manifests]
    available = availability_map(labels)
    records = {record.app_label: record for record in AppInstallation.objects.filter(app_label__in=labels)}
    states = {}
    for manifest in manifests:
        record = records.get(manifest.app_label)
        stored = record.configuration if record else {}
        try:
            configuration, configuration_valid = validate_configuration(manifest, stored), True
        except ValidationError:
            # The stored value no longer fits the manifest (#251): show the defaults, close the app
            # in this organization and never rewrite the row over HTTP (a data migration fixes it).
            configuration, configuration_valid = validate_configuration(manifest, {}), False
        states[manifest.app_label] = {
            "app_label": manifest.app_label,
            "available": available[manifest.app_label],
            "label": manifest.label,
            "description": manifest.description,
            "app_id": manifest.app_id,
            "version": manifest.version,
            # Closed by default (#22): the obsolete manifest default_enabled no longer activates anything.
            "enabled": record.enabled if record else False,
            "configuration": configuration,
            "configuration_valid": configuration_valid,
            "revision": record.revision if record else 0,
            "sysid": str(record.pk) if record else None,
        }
    for label, state in states.items():
        closure = [label, *required_labels(label)]
        state["effective"] = all(
            dependency in states
            and states[dependency]["available"]
            and states[dependency]["enabled"]
            and states[dependency]["configuration_valid"]
            for dependency in closure
        )
    return states


def application_states():
    """Availability, activation and configuration of every installed app in the current tenant.

    A fixed number of queries whatever the number of apps (availability, grants and
    installations), memoized for the current request only and never across requests.
    """
    from skeleton.tenancy.context import request_cache

    if current_context().organization_id is None:
        raise MissingTenantContext("Application state requires tenant context.")
    cache = request_cache()
    key = _memo_key()
    if cache is None or cache.get(key) is _STALE or cache.get((_MEMO, "global")) is _STALE:
        return _load_states()
    if key not in cache:
        cache[key] = _load_states()
    return cache[key]


def application_state(app_label):
    get_app(app_label)  # KeyError for an app that is not installed, as before.
    state = application_states()[app_label]
    return {**state, "configuration": dict(state["configuration"])}


def require_application_enabled(app_label):
    try:
        manifest = get_app(app_label)
    except KeyError:
        return  # The core has no optional tenant activation contract.
    states = application_states()
    # Transitive: a dependency's own disabled or unavailable dependency also closes this application.
    for label in (manifest.app_label, *required_labels(manifest.app_label)):
        state = states.get(label)
        dependency = label != manifest.app_label
        if state is None or not state["available"]:
            raise ApplicationUnavailable(
                "Application dependency is unavailable."
                if dependency
                else gettext("O aplicativo não está disponível para esta organização.")
            )
        if not state["configuration_valid"]:
            raise PermissionDenied(
                "Application dependency configuration is incompatible."
                if dependency
                else "Application configuration is incompatible with the installed version."
            )
        if not state["enabled"]:
            raise PermissionDenied(
                gettext("Uma dependência do aplicativo está desativada.")
                if dependency
                else gettext("O aplicativo está desativado nesta organização.")
            )


@transaction.atomic
def configure_application(app_label, *, user, enabled, configuration, revision):
    from skeleton.access.capabilities import APPS_MANAGE, require_capability
    from skeleton.tenancy.models import Organization

    from .models import AppInstallation

    # The app manager (#17) administers activation and parameters without full administration.
    require_capability(user, APPS_MANAGE)
    manifest = get_app(app_label)
    validate_dependencies()
    if type(enabled) is not bool:
        raise ValidationError({"enabled": gettext("Use true or false.")})
    values = validate_configuration(manifest, configuration)
    # Serialize even the first virtual revision=0 transition and dependency changes.
    Organization.objects.select_for_update(no_key=True).get(pk=current_context().organization_id)
    record = AppInstallation.objects.filter(app_label=app_label).first()
    expected = record.revision if record else 0
    if type(revision) is not int or revision != expected:
        raise ConcurrentUpdate("Application configuration changed.")
    if record is not None:
        try:
            validate_configuration(manifest, record.configuration)
        except ValidationError:
            # Nothing is written while the stored row is incompatible (#251): the app is closed
            # until the operator's data migration fixes it.
            raise ValidationError(
                {
                    "configuration": ValidationError(
                        "Stored configuration is incompatible with the installed version; a data migration is required.",
                        code="configuration_incompatible",
                    )
                }
            ) from None
    from .availability import available_with_dependencies

    forget_application_states()
    if enabled and not available_with_dependencies(app_label):
        # Disabling stays allowed so an organization can close an app the platform withdrew.
        raise ValidationError(
            {
                "enabled": ValidationError(
                    gettext("O aplicativo não está disponível para esta organização."), code="app_unavailable"
                )
            }
        )
    if enabled:
        pending = [label for label in required_labels(app_label) if not application_state(label)["enabled"]]
        if pending:
            names = ", ".join(get_app(label).label for label in pending)
            raise ValidationError(
                {"enabled": gettext("Ative antes os aplicativos exigidos: %(names)s.") % {"names": names}}
            )
    else:
        blocking = [label for label in dependent_labels(app_label) if application_state(label)["enabled"]]
        if blocking:
            names = ", ".join(get_app(label).label for label in blocking)
            raise ValidationError(
                {"enabled": gettext("Desative antes os aplicativos dependentes: %(names)s.") % {"names": names}}
            )
    from skeleton.events.core_events import application_changed

    was_enabled = bool(record and record.enabled)
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
    application_changed(record, was_enabled=was_enabled)
    return record


def navigation_for_user(user):
    """Menu entries of usable apps whose table the user may act on, in a fixed number of queries.

    The same gates as ``authorized_queryset`` (activation of the app and of the model's app,
    exposed ``SecuredRecord``, table permission and token scope), evaluated in batch. A link
    grants nothing: every page revalidates on the server.
    """
    from skeleton.access.policies import table_permissions
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import get_spec

    states = application_states()
    entries = [
        (manifest, entry)
        for manifest in all_apps()
        if states.get(manifest.app_label, {}).get("effective")
        for entry in manifest.navigation
    ]
    if not entries:
        return []
    allowed = table_permissions(user)
    result = []
    for manifest, entry in entries:
        try:
            spec = get_spec(entry.table_key)
        except (KeyError, LookupError):
            continue
        owner = spec.model._meta.app_label
        if owner in states and not states[owner]["effective"]:
            continue
        if not issubclass(spec.model, SecuredRecord) or not spec.expose_api:
            continue
        if (spec.model._meta.label_lower, entry.action) not in allowed:
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
    from skeleton.access.capabilities import APPS_MANAGE, capabilities_for
    from skeleton.access.policies import is_organization_admin

    return {
        "application_navigation": navigation_for_user(request.user),
        "can_manage_applications": APPS_MANAGE in capabilities_for(request.user),
        "can_manage_organization": is_organization_admin(request.user),
    }
