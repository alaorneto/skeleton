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
    from skeleton.common.entitlements import entitled_apps

    from .availability import platform_availability_map
    from .models import AppInstallation

    manifests = all_apps()
    labels = [manifest.app_label for manifest in manifests]
    offered = platform_availability_map(labels)
    entitled = entitled_apps(current_context().organization_id, [label for label in labels if offered[label]])
    # Effective availability (#395): offered by the platform and, with billing, paid for.
    available = {label: offered[label] and entitled.get(label, True) for label in labels}
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
            # Offered by the platform but not contracted: the apps page offers to contract it (#404).
            "contract_required": offered[manifest.app_label] and not available[manifest.app_label],
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


def require_activation_allowed(app_label, *, enabled):
    """Raise ValidationError unless the current organization may switch ``app_label`` to ``enabled`` (#282).

    No actor authorization here: callers authorize first (configure_application) or run as
    infrastructure under system_context (CLI). Reads the organization of the current context.
    """
    from skeleton.common.entitlements import entitled_apps
    from skeleton.common.exceptions import ApplicationNotEntitled

    from .availability import available_with_dependencies, platform_availability_map

    forget_application_states()
    if enabled:
        closure = [app_label, *required_labels(app_label)]
        organization_id = current_context().organization_id
        offered = platform_availability_map(closure, organization_id=organization_id)
        if all(offered.values()):
            missing = [label for label, ok in entitled_apps(organization_id, closure).items() if not ok]
            if missing:
                # Offered by the platform but not paid for (#395): the surfaces offer to contract it.
                names = ", ".join(get_app(label).label for label in missing)
                raise ApplicationNotEntitled(
                    {
                        "enabled": ValidationError(
                            gettext("Contrate o aplicativo para ativá-lo: %(names)s.") % {"names": names},
                            code="app_not_entitled",
                        )
                    }
                )
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


@transaction.atomic
def activate_for_entitlement(app_label):
    """Turn ``app_label`` on in the current organization because billing granted the right (#396).

    Infrastructure only (``system_context`` of the organization, no actor): the same lock,
    availability and dependency checks as ``configure_application``, the stored configuration
    kept (defaults for a first activation) and the same ``app.enabled`` event. Already enabled
    is a no-op; a refusal raises ``ValidationError``.
    """
    return _activate_by_infrastructure(app_label)


def _activate_by_infrastructure(app_label):
    """The activation shared by billing (#396) and the platform (#699), in the current organization."""
    from skeleton.access.services import organization_lock
    from skeleton.events.core_events import application_changed

    from .models import AppInstallation

    manifest = get_app(app_label)
    with organization_lock():
        record = AppInstallation.objects.filter(app_label=app_label).first()
        if record is not None and record.enabled:
            return record
        values = validate_configuration(manifest, record.configuration if record else {})
        require_activation_allowed(app_label, enabled=True)
        if record is None:
            record = AppInstallation(
                organization_id=current_context().organization_id,
                app_label=app_label,
                enabled=True,
                configuration=values,
            )
            record.save()
        else:
            record.enabled = True
            record.save(update_fields={"enabled"})
        forget_application_states(everywhere=True)
        application_changed(record, was_enabled=False)
        return record


def _deactivate_by_infrastructure(app_label):
    """Turn ``app_label`` off in the current organization, keeping its configuration (#699)."""
    from skeleton.access.services import organization_lock
    from skeleton.events.core_events import application_changed

    from .models import AppInstallation

    get_app(app_label)
    with organization_lock():
        record = AppInstallation.objects.filter(app_label=app_label).first()
        if record is None or not record.enabled:
            return record
        require_activation_allowed(app_label, enabled=False)
        record.enabled = False
        record.save(update_fields={"enabled"})
        forget_application_states(everywhere=True)
        application_changed(record, was_enabled=True)
        return record


PLATFORM_APPS_REASON = "platform:apps"


@transaction.atomic
def assign_app(app_label, *, user, organization_id):
    """The platform gives one organization an app (#699): offers it and activates it with its dependencies.

    In ``selected`` mode the app (and each dependency not yet offered) is granted to the
    organization; then, in the system context of that organization only, the dependencies and
    the app are activated with the same checks as an activation by the organization (availability,
    billing entitlement, dependencies). An app already effective changes nothing.
    """
    from django.utils.translation import gettext

    from skeleton.accounts.platform import require_platform_context
    from skeleton.common.exceptions import ApplicationNotEntitled
    from skeleton.tenancy.context import system_context

    from .availability import grant_app, platform_availability_map
    from .models import AppAvailability

    require_platform_context(user)
    labels = [*required_labels(app_label), app_label]  # Dependencies first.
    for label in labels:
        get_app(label)
        if not AppAvailability.objects.filter(app_label=label).exists():
            raise ValidationError(
                {
                    "app_label": ValidationError(
                        gettext("Defina a disponibilidade da app antes."), code="no_availability"
                    )
                }
            )
    for label in labels:
        if not platform_availability_map([label], organization_id=organization_id)[label]:
            grant_app(label, user=user, organization_id=organization_id)
    with system_context(reason=PLATFORM_APPS_REASON, organization_id=organization_id):
        try:
            for label in labels:
                _activate_by_infrastructure(label)
        except ApplicationNotEntitled as refused:
            message = gettext("App paga: use a cobrança da organização.")
            raise ApplicationNotEntitled({"app_label": ValidationError(message, code="app_not_entitled")}) from refused


@transaction.atomic
def unassign_app(app_label, *, user, organization_id, revoke_grant=True):
    """The platform takes an app back from one organization (#699).

    Deactivates it (refused while a dependent app is active there; the message lists them),
    keeps its configuration and data and, in ``selected`` mode with ``revoke_grant``, also
    revokes the grant. Nothing of the app's data is deleted.
    """
    from skeleton.accounts.platform import require_platform_context
    from skeleton.tenancy.context import system_context

    from .availability import revoke_app
    from .models import AppAvailability

    require_platform_context(user)
    get_app(app_label)
    with system_context(reason=PLATFORM_APPS_REASON, organization_id=organization_id):
        _deactivate_by_infrastructure(app_label)
    mode = AppAvailability.objects.filter(app_label=app_label).values_list("mode", flat=True).first()
    if revoke_grant and mode == AppAvailability.Mode.SELECTED:
        revoke_app(app_label, user=user, organization_id=organization_id)


def platform_fields(manifest):
    """The configuration fields only the platform writes (``managed_by: "platform"``, #702)."""
    return tuple(field for field in manifest.configuration_fields if field.managed_by == "platform")


def _keep_platform_fields(manifest, configuration):
    """The organization's configuration with the platform's fields as they are stored (#702).

    An absent platform field keeps the stored value; a different one is refused with
    ``setting_locked``, before anything is written. Infrastructure is not restricted.
    """
    from .models import AppInstallation

    managed = platform_fields(manifest)
    if not managed or not isinstance(configuration, dict) or current_context().system:
        return configuration
    record = AppInstallation.objects.filter(app_label=manifest.app_label).first()
    stored = validate_configuration(manifest, record.configuration if record else {})
    result = dict(configuration)
    for field in managed:
        if field.name not in result:
            result[field.name] = stored[field.name]
        elif result[field.name] != stored[field.name]:
            message = gettext("Definido pela plataforma: %(name)s.") % {"name": field.label or field.name}
            raise ValidationError({"configuration": ValidationError(message, code="setting_locked")})
    return result


@transaction.atomic
def configure_application_by_platform(app_label, *, user, organization_id, values, revision):
    """The platform administrator writes the platform's fields of one organization (#702).

    Only fields with ``managed_by: "platform"`` are accepted, validated by the manifest; CAS by
    the revision of the organization's ``AppInstallation`` (0 before it exists: the row is
    created disabled with the defaults). Opens the system context of that organization only.
    """
    from skeleton.accounts.platform import require_platform_context
    from skeleton.tenancy.context import system_context

    from .models import AppInstallation

    require_platform_context(user)
    manifest = get_app(app_label)
    allowed = {field.name for field in platform_fields(manifest)}
    if not isinstance(values, dict) or set(values) - allowed:
        raise ValidationError(
            {"configuration": gettext("Só os campos geridos pela plataforma podem ser gravados aqui.")}
        )
    if type(revision) is not int:
        raise ConcurrentUpdate("Application configuration changed.")
    from skeleton.access.services import organization_lock

    with system_context(reason=PLATFORM_FIELDS_REASON, organization_id=organization_id), organization_lock():
        record = AppInstallation.objects.select_for_update().filter(app_label=app_label).first()
        if (record.revision if record else 0) != revision:
            raise ConcurrentUpdate("Application configuration changed.")
        stored = validate_configuration(manifest, record.configuration if record else {})
        merged = validate_configuration(manifest, {**stored, **values})
        if record is None:
            record = AppInstallation(
                organization_id=organization_id, app_label=app_label, enabled=False, configuration=merged
            )
            record.save()
        elif record.configuration != merged:
            record.configuration = merged
            record.save(update_fields={"configuration"})
        forget_application_states(everywhere=True)
        return record


PLATFORM_FIELDS_REASON = "platform:setting-lock"


@transaction.atomic
def configure_application(app_label, *, user, enabled, configuration, revision):
    from skeleton.access.capabilities import APPS_MANAGE, require_capability
    from skeleton.access.services import organization_lock

    from .models import AppInstallation

    # The app manager (#17) administers activation and parameters without full administration.
    require_capability(user, APPS_MANAGE)
    manifest = get_app(app_label)
    validate_dependencies()
    if type(enabled) is not bool:
        raise ValidationError({"enabled": gettext("Use true or false.")})
    configuration = _keep_platform_fields(manifest, configuration)
    values = validate_configuration(manifest, configuration)
    from skeleton.tenancy.setting_locks import require_unlocked

    # Activation and fields the platform locked (#701) keep their values.
    require_unlocked(
        {f"apps.{app_label}.enabled": enabled, **{f"apps.{app_label}.{name}": value for name, value in values.items()}},
        field_of={
            f"apps.{app_label}.enabled": "enabled",
            **{f"apps.{app_label}.{name}": "configuration" for name in values},
        },
    )
    # Serialize even the first virtual revision=0 transition and dependency changes. The lock
    # re-validates the organization and the actor's capability once it is held (#253).
    with organization_lock(user, capability=APPS_MANAGE):
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
        require_activation_allowed(app_label, enabled=enabled)
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


def navigation_for_user(user, *, permissions=None):
    """Menu entries of usable apps whose table the user may act on, in a fixed number of queries.

    The same gates as ``authorized_queryset`` (activation of the app and of the model's app,
    exposed ``SecuredRecord``, table permission and token scope), evaluated in batch. A link
    grants nothing: every page revalidates on the server. ``permissions`` is the result of
    ``table_permissions(user)`` when the caller already read it in this request.
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
    allowed = permissions if permissions is not None else table_permissions(user)
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


def active_applications_for(user):
    """Apps a member sees as usable in the current tenant (#605): effective and visible to the role.

    An app is listed when it is effective (installed, available, enabled, with its dependencies)
    and either declares no navigation or has at least one entry the role may act on, with the
    same gates as the menu (``navigation_for_user``). Nothing about licensing or billing.
    """
    states = application_states()
    visible = {entry["app_label"] for entry in navigation_for_user(user)}
    result = []
    for manifest in sorted(all_apps(), key=lambda item: item.app_label):
        if not states.get(manifest.app_label, {}).get("effective"):
            continue
        if manifest.navigation and manifest.app_label not in visible:
            continue
        result.append(
            {
                "app_id": manifest.app_id or manifest.app_label,
                "label": manifest.app_label,
                "version": manifest.version,
                "title": str(manifest.label),
            }
        )
    return result


def application_navigation(request):
    context = current_context()
    if context.organization_id is None or not getattr(request.user, "is_authenticated", False):
        return {}
    from django.conf import settings

    from skeleton.access.capabilities import APPS_MANAGE, BILLING_VIEW, capabilities_for
    from skeleton.access.policies import is_organization_admin, table_permissions

    capabilities = capabilities_for(request.user)
    is_admin = is_organization_admin(request.user)
    permissions = table_permissions(request.user)
    # Read once per render: the shell processor that runs next (skeleton.web.navigation) reuses
    # these values for the settings area (#621) instead of querying them again.
    request.skeleton_navigation_facts = {
        "user_id": request.user.pk,
        "organization_id": context.organization_id,
        "capabilities": capabilities,
        "is_admin": is_admin,
        "table_permissions": permissions,
    }
    return {
        "application_navigation": navigation_for_user(request.user, permissions=permissions),
        "can_manage_applications": APPS_MANAGE in capabilities,
        "can_manage_organization": is_admin,
        # The billing area (#404), only when the platform is sold with billing on.
        "can_view_billing": bool(getattr(settings, "SKELETON_BILLING_ENABLED", False)) and BILLING_VIEW in capabilities,
    }
