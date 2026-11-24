"""Lockable settings of apps (#701): ``apps.<label>.enabled`` and ``apps.<label>.<field>``.

Registered as a provider of ``skeleton.tenancy.setting_locks`` in ``SystemConfig.ready``. The
values are applied in the system context of one organization, with the same checks as an
activation or a configuration by the organization (availability, entitlement, dependencies,
declared types); the organization's own ``configure_application`` then refuses to change them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext


def _state(app_label: str) -> dict[str, Any]:
    from .applications import application_state

    return application_state(app_label)


def _enabled_read(app_label: str) -> Callable[[], Any]:
    return lambda: bool(_state(app_label)["enabled"])


def _enabled_apply(app_label: str) -> Callable[[Any], None]:
    def apply(value: Any) -> None:
        from .applications import activate_for_entitlement

        if value:
            activate_for_entitlement(app_label)
        else:
            deactivate_by_infrastructure(app_label)

    return apply


def deactivate_by_infrastructure(app_label: str) -> Any:
    """Turn the app off in the current organization, keeping its configuration (system context)."""
    from skeleton.access.services import organization_lock
    from skeleton.events.core_events import application_changed

    from .app_registry import get_app, require_not_library
    from .applications import forget_application_states, require_activation_allowed
    from .models import AppInstallation

    get_app(app_label)
    require_not_library(app_label)
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


def _bool(value: Any) -> Any:
    if type(value) is not bool:
        raise ValidationError({"value": gettext("Use verdadeiro ou falso.")})
    return value


def _field_normalize(manifest: Any, field: Any) -> Callable[[Any], Any]:
    def normalize(value: Any) -> Any:
        if field.value_type is bool:
            return _bool(value)
        if field.value_type is int and isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                raise ValidationError({"value": gettext("Use um número inteiro.")}) from None
        if type(value) is not field.value_type or (isinstance(value, str) and len(value) > field.max_length):
            raise ValidationError({"value": gettext("Valor inválido para %(name)s.") % {"name": field.name}})
        return value

    return normalize


def _field_read(app_label: str, name: str) -> Callable[[], Any]:
    return lambda: _state(app_label)["configuration"].get(name)


def _field_apply(app_label: str, name: str) -> Callable[[Any], None]:
    def apply(value: Any) -> None:
        write_configuration_value(app_label, name, value)

    return apply


def write_configuration_value(app_label: str, name: str, value: Any) -> Any:
    """Write one configuration field of the app in the current organization, without an actor.

    The stored configuration is validated as a whole by the manifest; an app never configured
    gets a disabled ``AppInstallation`` with the defaults and this value.
    """
    from skeleton.access.services import organization_lock
    from skeleton.tenancy.context import current_context

    from .app_registry import get_app, validate_configuration
    from .applications import forget_application_states
    from .models import AppInstallation

    manifest = get_app(app_label)
    with organization_lock():
        record = AppInstallation.objects.select_for_update().filter(app_label=app_label).first()
        current = record.configuration if record is not None else {}
        values = validate_configuration(manifest, {**validate_configuration(manifest, current), name: value})
        if record is None:
            record = AppInstallation(
                organization_id=current_context().organization_id,
                app_label=app_label,
                enabled=False,
                configuration=values,
            )
            record.save()
        elif record.configuration != values:
            record.configuration = values
            record.save(update_fields={"configuration"})
        forget_application_states(everywhere=True)
        return record


def _kind(field: Any) -> str:
    return {bool: "bool", int: "int"}.get(field.value_type, "str")


def apps_provider() -> Iterable[Any]:
    from skeleton.tenancy.setting_locks import LockableSetting

    from .app_registry import all_apps

    for manifest in sorted(all_apps(), key=lambda item: item.app_label):
        if manifest.library:
            continue  # No activation nor configuration to lock: it follows its dependents (#792).
        label = manifest.app_label
        group = gettext("App %(name)s") % {"name": manifest.label}
        yield LockableSetting(
            f"apps.{label}.enabled",
            gettext("Ativada"),
            group,
            "bool",
            _bool,
            _enabled_read(label),
            _enabled_apply(label),
        )
        for field in manifest.configuration_fields:
            if field.name == "enabled" or field.managed_by == "platform":
                continue  # The activation key above; platform fields are the platform's already (#702).
            yield LockableSetting(
                f"apps.{label}.{field.name}",
                field.label or field.name,
                group,
                _kind(field),
                _field_normalize(manifest, field),
                _field_read(label, field.name),
                _field_apply(label, field.name),
            )


def register_app_setting_provider() -> None:
    from skeleton.tenancy.setting_locks import register_setting_provider

    register_setting_provider("apps", apps_provider)
