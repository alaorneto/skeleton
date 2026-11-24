"""The organization's configuration of notifications (#785).

Whoever holds ``notifications.manage`` sees the types the organization can receive (the core's and
those of its active apps), turns off for every member a type of the organization or app context
that is not mandatory, and writes the organization's text per language. The platform's types are
read only: the organization never turns them off nor rewrites them. A lock of the platform
(``notifications.<type>.enabled``, ``notifications.<type>.text``) refuses the write with
``setting_locked``. Every write runs in the request's tenant, with CAS on the live row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context


class UnknownType(LookupError):
    """The type does not exist here (unknown, or of an app that is not active): the view answers 404."""


@dataclass
class OrganizationTypeConfig:
    kind: Any
    enabled: bool
    reason: str  # "", "mandatory", "instance_disabled", "organization_disabled", "platform_type"
    locked_enabled: bool
    locked_text: bool
    can_disable: bool
    can_edit_text: bool
    setting: Any = None
    texts: dict[str, Any] = field(default_factory=dict)
    has_text: bool = False


def enabled_key(type_code: str) -> str:
    return f"notifications.{type_code}.enabled"


def text_key(type_code: str) -> str:
    return f"notifications.{type_code}.text"


def require_manager(actor: Any) -> None:
    from skeleton.access.capabilities import NOTIFICATIONS_MANAGE, has_capability

    if current_context().organization_id is None or not has_capability(actor, NOTIFICATIONS_MANAGE):
        raise PermissionDenied("notifications.manage required.")


def _active_apps() -> frozenset[str]:
    from skeleton.system.applications import application_states

    return frozenset(label for label, state in application_states().items() if state.get("effective"))


def available(kind: Any, active_apps: frozenset[str] | None = None) -> bool:
    """A type of the core always; an app's only while the app is active in this organization."""
    from .registry import is_core_owner

    if kind.audience == "platform":
        return False
    if not kind.owner_app or is_core_owner(kind.owner_app):
        return True
    return kind.owner_app in (active_apps if active_apps is not None else _active_apps())


def kind_for_organization(type_code: Any) -> Any:
    from .registry import get_notification_type

    try:
        kind = get_notification_type(str(type_code))
    except KeyError:
        raise UnknownType(type_code) from None
    if not available(kind):
        raise UnknownType(type_code)
    return kind


def settings_rows() -> dict[str, Any]:
    from .models import OrganizationNotificationSetting

    if current_context().organization_id is None:
        return {}
    return {row.type_code: row for row in OrganizationNotificationSetting.objects.all()}


def organization_disabled(kind: Any) -> bool:
    """Whether the context organization turned ``kind`` off (never a mandatory or platform type)."""
    from .models import OrganizationNotificationSetting
    from .registry import org_can_disable

    if current_context().organization_id is None or not org_can_disable(kind):
        return False
    return OrganizationNotificationSetting.objects.filter(type_code=kind.code, enabled=False).exists()


def organization_texts(type_code: str) -> dict[str, tuple[str, str]]:
    """``{language: (title, body)}`` of the context organization; nothing outside a tenant."""
    from .models import OrganizationNotificationText

    if current_context().organization_id is None:
        return {}
    rows = OrganizationNotificationText.objects.filter(type_code=type_code).values_list("language", "title", "body")
    return {language: (title, body) for language, title, body in rows}


def text_rows(type_code: str) -> dict[str, Any]:
    from .models import OrganizationNotificationText

    return {row.language: row for row in OrganizationNotificationText.objects.filter(type_code=type_code)}


def _config(
    kind: Any, *, rows: dict[str, Any], instance: dict[str, Any], locked: dict[str, Any]
) -> OrganizationTypeConfig:
    from .instance_settings import DEFAULT_STATE, enabled_in_instance
    from .registry import org_can_disable, org_can_edit_text, type_context

    row = rows.get(kind.code)
    if kind.mandatory:
        enabled, reason = True, "mandatory"
    elif not enabled_in_instance(kind, instance.get(kind.code, DEFAULT_STATE)):
        enabled, reason = False, "instance_disabled"
    elif org_can_disable(kind) and row is not None and not row.enabled:
        enabled, reason = False, "organization_disabled"
    else:
        enabled, reason = True, "platform_type" if type_context(kind) == "platform" else ""
    return OrganizationTypeConfig(
        kind=kind,
        enabled=enabled,
        reason=reason,
        locked_enabled=enabled_key(kind.code) in locked,
        locked_text=text_key(kind.code) in locked,
        can_disable=org_can_disable(kind),
        can_edit_text=org_can_edit_text(kind),
        setting=row,
    )


def organization_overview(*, actor: Any) -> list[OrganizationTypeConfig]:
    """Every type the organization can receive, with its state and why; a fixed number of queries."""
    from skeleton.tenancy.setting_locks import locked_values

    from .instance_settings import instance_state
    from .models import OrganizationNotificationText
    from .registry import all_notification_types

    require_manager(actor)
    active = _active_apps()
    rows, instance, locked = settings_rows(), instance_state(), locked_values()
    with_text = set(OrganizationNotificationText.objects.values_list("type_code", flat=True))
    configs = []
    for kind in all_notification_types():
        if available(kind, active):
            config = _config(kind, rows=rows, instance=instance, locked=locked)
            config.has_text = kind.code in with_text
            configs.append(config)
    return configs


def type_config(*, actor: Any, type_code: str) -> OrganizationTypeConfig:
    from skeleton.tenancy.setting_locks import locked_values

    from .instance_settings import instance_state

    require_manager(actor)
    kind = kind_for_organization(type_code)
    config = _config(kind, rows=settings_rows(), instance=instance_state([kind.code]), locked=locked_values())
    config.texts = text_rows(kind.code)
    return config


def _check_revision(expected_revision: Any, record: Any) -> None:
    if record is None:
        if expected_revision is not None:
            raise ConcurrentUpdate("The organization notification setting is gone.")
        return
    if type(expected_revision) is not int or record.revision != expected_revision:
        raise ConcurrentUpdate("The organization notification setting changed.")


def _refuse(field_name: str, message: str, code: str) -> ValidationError:
    return ValidationError({field_name: ValidationError(message, code=code)})


def _write_enabled(kind: Any, enabled: bool, expected_revision: Any, *, check: bool = True) -> Any:
    from .models import OrganizationNotificationSetting

    with transaction.atomic():
        record = OrganizationNotificationSetting.objects.select_for_update().filter(type_code=kind.code).first()
        if check:
            _check_revision(expected_revision, record)
        if record is None:
            try:
                with transaction.atomic():
                    record = OrganizationNotificationSetting(
                        organization_id=current_context().organization_id, type_code=kind.code, enabled=enabled
                    )
                    record.save()
            except IntegrityError:
                raise ConcurrentUpdate("The organization notification setting was created concurrently.") from None
        elif record.enabled != enabled:
            record.enabled = enabled
            record.save(update_fields={"enabled"})
    return record


def set_enabled(*, actor: Any, type_code: str, enabled: bool, expected_revision: int | None) -> Any:
    """Turn ``type_code`` on or off for every member of the organization."""
    from skeleton.tenancy.setting_locks import require_unlocked

    from .instance_settings import enabled_in_instance
    from .registry import type_context

    require_manager(actor)
    kind = kind_for_organization(type_code)
    if type(enabled) is not bool:
        raise _refuse("enabled", gettext("Informe se o aviso é enviado."), "invalid")
    if type_context(kind) == "platform":
        raise _refuse("enabled", gettext("Este aviso é definido pela plataforma."), "platform_type")
    if kind.mandatory:
        raise _refuse("enabled", gettext("Um aviso obrigatório não pode ser desligado."), "mandatory")
    if enabled and not enabled_in_instance(kind):
        raise _refuse("enabled", gettext("A plataforma desligou este aviso."), "instance_disabled")
    require_unlocked({enabled_key(kind.code): enabled}, field_of={enabled_key(kind.code): "enabled"})
    return _write_enabled(kind, enabled, expected_revision)


def _editable(kind: Any) -> None:
    from .registry import type_context

    if type_context(kind) == "platform":
        raise _refuse("title", gettext("Este aviso é definido pela plataforma."), "platform_type")
    if not kind.org_editable or kind.fixed_text:
        raise _refuse("title", gettext("O texto deste aviso não pode ser alterado."), "not_editable")


def set_text(*, actor: Any, type_code: str, language: str, title: str, body: str, expected_revision: int | None) -> Any:
    from skeleton.tenancy.setting_locks import require_keys_unlocked

    from .instance_settings import languages, validate_text
    from .models import OrganizationNotificationText

    require_manager(actor)
    kind = kind_for_organization(type_code)
    _editable(kind)
    code = str(language or "").lower()
    if code not in languages():
        raise _refuse("language", gettext("Idioma não disponível nesta instância."), "invalid")
    require_keys_unlocked(text_key(kind.code), field="title")
    title, body = validate_text(kind, title, body)
    with transaction.atomic():
        record = (
            OrganizationNotificationText.objects.select_for_update().filter(type_code=kind.code, language=code).first()
        )
        _check_revision(expected_revision, record)
        if record is None:
            try:
                with transaction.atomic():
                    record = OrganizationNotificationText(
                        organization_id=current_context().organization_id,
                        type_code=kind.code,
                        language=code,
                        title=title,
                        body=body,
                    )
                    record.save()
            except IntegrityError:
                raise ConcurrentUpdate("The organization notification text was created concurrently.") from None
            return record
        record.title, record.body = title, body
        record.save(update_fields={"title", "body"})
    return record


def clear_text(*, actor: Any, type_code: str, language: str, expected_revision: int) -> None:
    """Back to the platform's or the factory text in ``language``."""
    from skeleton.tenancy.setting_locks import require_keys_unlocked

    from .models import OrganizationNotificationText

    require_manager(actor)
    kind = kind_for_organization(type_code)
    _editable(kind)
    require_keys_unlocked(text_key(kind.code), field="title")
    with transaction.atomic():
        record = (
            OrganizationNotificationText.objects.select_for_update()
            .filter(type_code=kind.code, language=str(language).lower())
            .first()
        )
        if record is None:
            raise ConcurrentUpdate("The organization notification text is gone.")
        _check_revision(expected_revision, record)
        record.delete()


# --------------------------------------------------------------------------- platform locks


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in ("true", "1", "on", "sim"):
        return True
    if str(value).lower() in ("false", "0", "off", "não", "nao", ""):
        return False
    raise ValidationError(gettext("Informe sim ou não."))


def _read_enabled(kind: Any):
    def read() -> bool:
        return not organization_disabled(kind)

    return read


def _apply_enabled(kind: Any):
    def apply(value: Any) -> None:
        # In the organization's system context (the lock itself): no capability nor lock check.
        _write_enabled(kind, bool(value), None, check=False)

    return apply


def _read_text(kind: Any):
    def read() -> Any:
        return None

    return read


def _noop(value: Any) -> None:
    return None


def notifications_provider():
    """``notifications.<type>.enabled`` and ``.text`` of every organization or app type the organization may change."""
    from skeleton.tenancy.setting_locks import LockableSetting

    from .registry import all_notification_types, org_can_disable, org_can_edit_text

    group = gettext("Notificações")
    for kind in all_notification_types():
        if kind.audience == "platform":
            continue
        if org_can_disable(kind):
            yield LockableSetting(
                enabled_key(kind.code), str(kind.label), group, "bool", _bool, _read_enabled(kind), _apply_enabled(kind)
            )
        if org_can_edit_text(kind):
            yield LockableSetting(
                text_key(kind.code),
                gettext("Texto de %(name)s") % {"name": kind.label},
                group,
                "frozen",
                lambda value: None,
                _read_text(kind),
                _noop,
            )


def register_notification_lock_provider() -> None:
    from skeleton.tenancy.setting_locks import register_setting_provider

    register_setting_provider("notifications", notifications_provider)
