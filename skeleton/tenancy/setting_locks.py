"""Settings of an organization locked by the platform (#701).

The platform administrator fixes a setting of one organization with a value and a reason. The
value is written to the organization (by the same code path its own service uses) and, while
the lock exists, every write by a person of that organization that would change it is refused
with ``setting_locked``: by page, REST API or service API. Infrastructure and the platform
(``system_context``) are not restricted: billing activating an app, the lock itself.

Lockable settings come from **providers** registered at ``ready()``: this package registers
``organization.*`` and ``admission.*``; ``skeleton.system`` registers ``apps.<label>.enabled``
and ``apps.<label>.<field>``. A provider is a callable that returns the settings it offers
now, so the apps installed at each moment are the ones offered.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context, request_cache, system_context

LOCK_REASON = "platform:setting-lock"
_MEMO = "skeleton.tenancy.setting_locks"
KINDS = ("str", "bool", "int", "text", "frozen")


@dataclass(frozen=True)
class LockableSetting:
    """One setting the platform may lock.

    ``kind`` says how a typed value is read from a form: ``str``, ``bool``, ``int``, ``text``
    (parsed by ``normalize``) or ``frozen`` (no value: the current one is kept, e.g. the logo).
    ``normalize`` validates and normalizes a value (``ValidationError``); ``read`` returns the
    current value and ``apply`` writes one, both in the organization's own context.
    """

    key: str
    label: str
    group: str
    kind: str
    normalize: Callable[[Any], Any]
    read: Callable[[], Any]
    apply: Callable[[Any], None]


_providers: dict[str, Callable[[], Iterable[LockableSetting]]] = {}


def register_setting_provider(prefix: str, provider: Callable[[], Iterable[LockableSetting]]) -> None:
    """Add a provider of lockable settings whose keys start with ``prefix.``; the same again is a no-op."""
    existing = _providers.get(prefix)
    if existing is not None and existing is not provider:
        raise ValueError(f"A setting provider for {prefix} is already registered.")
    _providers[prefix] = provider


def lockable_settings() -> dict[str, LockableSetting]:
    """Every lockable setting offered now, by key, in key order."""
    result: dict[str, LockableSetting] = {}
    for prefix, provider in sorted(_providers.items()):
        for setting in provider():
            if not setting.key.startswith(prefix + "."):
                raise ValueError(f"Setting {setting.key} is outside its provider prefix {prefix}.")
            if setting.key in result:
                raise ValueError(f"Setting {setting.key} is registered twice.")
            if setting.kind not in KINDS:
                raise ValueError(f"Setting {setting.key} has an unknown kind {setting.kind}.")
            result[setting.key] = setting
    return dict(sorted(result.items()))


def parse_form_value(setting: LockableSetting, raw: str) -> Any:
    """A value typed in a form, for ``setting.kind``; ``ValidationError`` when it does not fit."""
    raw = (raw or "").strip()
    if setting.kind == "frozen":
        return None
    if setting.kind == "bool":
        lowered = raw.lower()
        if lowered in ("true", "sim", "1", "on", "yes"):
            return True
        if lowered in ("false", "não", "nao", "0", "off", "no"):
            return False
        raise ValidationError({"value": gettext("Use sim ou não.")})
    if setting.kind == "int":
        try:
            return int(raw)
        except ValueError:
            raise ValidationError({"value": gettext("Use um número inteiro.")}) from None
    return raw


def _forget() -> None:
    cache = request_cache()
    if cache is not None:
        for key in [key for key in cache if isinstance(key, tuple) and key and key[0] == _MEMO]:
            del cache[key]


def locked_values(organization_id: UUID | None = None) -> dict[str, Any]:
    """Key → locked value of the organization (the context's by default); memoized per request."""
    from .models import OrganizationSettingLock

    organization_id = organization_id or current_context().organization_id
    if organization_id is None:
        return {}
    cache = request_cache()
    memo = (_MEMO, current_context().using, organization_id)
    if cache is not None and memo in cache:
        return dict(cache[memo])
    values = dict(
        OrganizationSettingLock.objects.filter(locked_organization_id=organization_id).values_list("key", "value")
    )
    if cache is not None:
        cache[memo] = values
    return dict(values)


def _same(first: Any, second: Any) -> bool:
    return json.dumps(first, sort_keys=True, default=str) == json.dumps(second, sort_keys=True, default=str)


def locked_refusal(field: str) -> ValidationError:
    return ValidationError({field: ValidationError(gettext("Definido pela plataforma."), code="setting_locked")})


def require_unlocked(changes: Mapping[str, Any], *, field_of: Mapping[str, str] | None = None) -> None:
    """Refuse a write by the organization that changes a locked setting.

    ``changes`` maps keys to the new values; a locked key whose new value equals the locked
    one passes (a form sends every field). ``field_of`` names the form field of each key for
    the error. Infrastructure and the platform (system context) are never restricted.
    """
    context = current_context()
    if context.system or context.organization_id is None:
        return
    locked = locked_values(context.organization_id)
    errors: dict[str, list[ValidationError]] = {}
    for key, value in changes.items():
        if key in locked and not _same(locked[key], value):
            field = (field_of or {}).get(key, key)
            errors.setdefault(field, []).append(
                ValidationError(gettext("Definido pela plataforma."), code="setting_locked")
            )
    if errors:
        raise ValidationError(errors)


def require_keys_unlocked(*keys: str, field: str) -> None:
    """Refuse any change of a ``frozen`` setting (logo, icon) while it is locked."""
    context = current_context()
    if context.system or context.organization_id is None:
        return
    if set(keys) & set(locked_values(context.organization_id)):
        raise locked_refusal(field)


def locked_fields(prefix: str) -> tuple[str, ...]:
    """Names (after ``prefix.``) of the locked settings of the context organization, sorted."""
    return tuple(sorted(key.removeprefix(prefix + ".") for key in locked_values() if key.startswith(prefix + ".")))


def _setting(key: str) -> LockableSetting:
    setting = lockable_settings().get(key)
    if setting is None:
        raise ValidationError({"key": gettext("Configuração desconhecida: %(key)s.") % {"key": key}})
    return setting


def _reason(reason: Any) -> str:
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > 500:
        raise ValidationError({"reason": gettext("Informe o motivo da trava (até 500 caracteres).")})
    return reason


@transaction.atomic
def lock_setting(
    *, actor: Any, organization: Any, key: str, value: Any, reason: str, expected_revision: int = 0
) -> Any:
    """Lock ``key`` of ``organization`` with ``value`` and write the value there.

    ``expected_revision=0`` creates or restores a removed lock (same row, same sysid); otherwise
    it is the revision of the live lock the page displayed. The value is applied in the system
    context of that organization only, by the same validators of the organization's service.
    """
    from skeleton.accounts.platform import require_platform_context

    from .models import OrganizationSettingLock

    require_platform_context(actor)
    setting = _setting(key)
    reason = _reason(reason)
    if type(expected_revision) is not int:
        raise ConcurrentUpdate("Setting lock changed.")
    if not organization.is_active or organization.deleted_at is not None:
        raise ValidationError({"organization": gettext("Organização indisponível.")})
    with system_context(reason=LOCK_REASON, organization_id=organization.pk):
        value = setting.read() if setting.kind == "frozen" else setting.normalize(value)
        if setting.kind != "frozen":
            setting.apply(value)
    row = (
        OrganizationSettingLock.all_objects.select_for_update()
        .filter(locked_organization_id=organization.pk, key=key)
        .first()
    )
    live = row is not None and row.deleted_at is None
    if (live and row.revision != expected_revision) or (not live and expected_revision != 0):
        raise ConcurrentUpdate("Setting lock changed.")
    _forget()
    if row is None:
        row = OrganizationSettingLock(locked_organization=organization, key=key, value=value, reason=reason)
        row.save()
        return row
    if not live:
        row.restore()
    changed = {name for name, new in (("value", value), ("reason", reason)) if not _same(getattr(row, name), new)}
    if changed:
        row.value, row.reason = value, reason
        row.save(update_fields=changed)
    return row


@transaction.atomic
def unlock_setting(*, actor: Any, organization: Any, key: str, expected_revision: int) -> None:
    """Remove the lock (soft delete): the organization may change the setting again; the value stays."""
    from skeleton.accounts.platform import require_platform_context

    from .models import OrganizationSettingLock

    require_platform_context(actor)
    row = (
        OrganizationSettingLock.objects.select_for_update()
        .filter(locked_organization_id=organization.pk, key=key)
        .first()
    )
    if row is None or type(expected_revision) is not int or row.revision != expected_revision:
        raise ConcurrentUpdate("Setting lock changed.")
    _forget()
    row.delete()


@dataclass(frozen=True)
class SettingRow:
    """One lockable setting of one organization on the platform page."""

    setting: LockableSetting
    current: Any
    lock: Any  # The live OrganizationSettingLock, or None.


def settings_overview(*, actor: Any, organization: Any) -> tuple[SettingRow, ...]:
    """Every lockable setting of the organization with its current value and lock (platform only).

    The current values are read in the read-only inspection context of the platform.
    """
    from skeleton.access.administration import read_only_inspection
    from skeleton.accounts.platform import require_context_actor, require_platform_admin

    from .models import OrganizationSettingLock

    require_platform_admin(actor)
    require_context_actor(actor)
    locks = {row.key: row for row in OrganizationSettingLock.objects.filter(locked_organization_id=organization.pk)}
    settings = lockable_settings()
    with (
        system_context(reason="platform:organization-inspect", organization_id=organization.pk),
        read_only_inspection("default"),
    ):
        return tuple(SettingRow(setting, setting.read(), locks.get(key)) for key, setting in settings.items())


# --------------------------------------------------------------------------- organization.* and admission.*


def _settings_value(name: str) -> Callable[[], Any]:
    def read() -> Any:
        from .organization_settings import current_settings

        record = current_settings()
        if record is None:
            from .models import OrganizationSettings

            return OrganizationSettings._meta.get_field(name).get_default()
        return getattr(record, name)

    return read


def _apply_settings(name: str) -> Callable[[Any], None]:
    def apply(value: Any) -> None:
        from .organization_settings import write_settings_field

        write_settings_field(name, value)

    return apply


def _normalize_settings(name: str) -> Callable[[Any], Any]:
    def normalize(value: Any) -> Any:
        from .organization_settings import clean_field

        return clean_field(name, value)

    return normalize


def _admission_value(name: str) -> Callable[[], Any]:
    def read() -> Any:
        from .admission import current_policy

        record = current_policy()
        return bool(getattr(record, name)) if record is not None else False

    return read


def _apply_admission(name: str) -> Callable[[Any], None]:
    def apply(value: Any) -> None:
        from .admission import write_policy_flag

        write_policy_flag(name, value)

    return apply


def _bool(field: str) -> Callable[[Any], Any]:
    def normalize(value: Any) -> Any:
        if type(value) is not bool:
            raise ValidationError({"value": gettext("Use verdadeiro ou falso.")})
        return value

    return normalize


def _frozen() -> Any:
    return None


def _noop(value: Any) -> None:
    return None


def _logo_value(field: str) -> Callable[[], Any]:
    def read() -> Any:
        from .organization_settings import current_settings

        record = current_settings()
        return bool(record is not None and getattr(record, field))

    return read


def organization_settings_provider() -> Iterable[LockableSetting]:
    labels = {
        "display_name": (gettext("Nome de exibição"), "str"),
        "default_language": (gettext("Idioma padrão"), "str"),
        "default_timezone": (gettext("Fuso horário padrão"), "str"),
        "brand_color": (gettext("Cor da marca"), "str"),
        "business_hours": (gettext("Horário de funcionamento"), "text"),
        "holidays": (gettext("Feriados"), "text"),
        "site_enabled": (gettext("Endereço próprio"), "bool"),
    }
    group = gettext("Organização")
    for name, (label, kind) in labels.items():
        yield LockableSetting(
            f"organization.{name}",
            label,
            group,
            kind,
            _normalize_settings(name),
            _settings_value(name),
            _apply_settings(name),
        )
    for name, field, label in (("logo", "logo_key", gettext("Logo")), ("icon", "icon_key", gettext("Ícone"))):
        yield LockableSetting(
            f"organization.{name}", label, group, "frozen", lambda value: None, _logo_value(field), _noop
        )


ADMISSION_FLAGS = {
    "discoverable": "Aparece no diretório",
    "join_requests_enabled": "Aceita pedidos de ingresso",
    "join_links_enabled": "Links de ingresso",
    "signup_via_link_enabled": "Cadastro pelo link",
    "domain_join_enabled": "Entrada por domínio verificado",
}


def admission_provider() -> Iterable[LockableSetting]:
    group = gettext("Admissão")
    for name, label in ADMISSION_FLAGS.items():
        yield LockableSetting(
            f"admission.{name}",
            gettext(label),
            group,
            "bool",
            _bool(name),
            _admission_value(name),
            _apply_admission(name),
        )


def register_tenancy_providers() -> None:
    register_setting_provider("organization", organization_settings_provider)
    register_setting_provider("admission", admission_provider)
