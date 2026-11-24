"""The platform's configuration of notifications in the whole instance (#784).

For each type the platform administrator may turn it off (never a mandatory one), choose the
channels it reaches by default (for whoever has not chosen otherwise) and write its text per
language. Writes are keyword-only, run in ``transaction.atomic()`` and require the revision of the
live row (CAS); ``actor=None`` is the console, as in the instance appearance.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

CHANNELS = ("in_app", "email", "push")
AUDIENCE_CHANNELS = {"members": CHANNELS, "external": ("email",), "platform": ("in_app", "email")}


@dataclass(frozen=True)
class InstanceState:
    enabled: bool
    default_channels: tuple[str, ...] | None  # None: the type's own default applies
    revision: int | None
    sysid: Any = None


DEFAULT_STATE = InstanceState(True, None, None)


def _require_platform(actor: Any) -> None:
    from skeleton.accounts.platform import is_platform_admin

    if actor is not None and not is_platform_admin(actor):
        raise PermissionDenied("Platform administration required.")


def _kind(type_code: Any) -> Any:
    from .registry import get_notification_type

    try:
        return get_notification_type(str(type_code))
    except KeyError:
        raise ValidationError({"type_code": gettext("Aviso desconhecido.")}) from None


def languages() -> tuple[str, ...]:
    """The languages a text may be written in: the instance's ``LANGUAGES``."""
    return tuple(code.lower() for code, _label in settings.LANGUAGES)


def allowed_channels(kind: Any) -> tuple[str, ...]:
    return AUDIENCE_CHANNELS.get(kind.audience, CHANNELS)


def instance_state(type_codes: Iterable[str] | None = None) -> dict[str, InstanceState]:
    """The live rows of ``type_codes`` (every type when ``None``) in one query."""
    from .models import InstanceNotificationSetting

    rows = InstanceNotificationSetting.objects.all()
    if type_codes is not None:
        rows = rows.filter(type_code__in=list(type_codes))
    return {
        row.type_code: InstanceState(row.enabled, tuple(row.default_channels), row.revision, row.pk) for row in rows
    }


def state_of(type_code: str) -> InstanceState:
    return instance_state([type_code]).get(type_code, DEFAULT_STATE)


def instance_channels(type_code: str) -> tuple[str, ...] | None:
    return state_of(type_code).default_channels


def enabled_in_instance(kind: Any, state: InstanceState | None = None) -> bool:
    """A mandatory type is always on; another one is on unless the platform turned it off."""
    if kind.mandatory:
        return True
    state = state if state is not None else state_of(kind.code)
    return state.enabled


def instance_texts(type_code: str) -> dict[str, tuple[str, str]]:
    """``{language: (title, body)}`` the platform wrote for ``type_code``."""
    from .models import InstanceNotificationText

    rows = InstanceNotificationText.objects.filter(type_code=type_code).values_list("language", "title", "body")
    return {language: (title, body) for language, title, body in rows}


def text_rows(type_code: str) -> dict[str, Any]:
    from .models import InstanceNotificationText

    return {row.language: row for row in InstanceNotificationText.objects.filter(type_code=type_code)}


def _check_revision(expected_revision: Any, record: Any) -> None:
    if record is None:
        if expected_revision is not None:
            raise ConcurrentUpdate("The instance notification setting is gone.")
        return
    if type(expected_revision) is not int or record.revision != expected_revision:
        raise ConcurrentUpdate("The instance notification setting changed.")


def _channels(kind: Any, default_channels: Any) -> list[str]:
    if not isinstance(default_channels, list | tuple) or not all(isinstance(item, str) for item in default_channels):
        raise ValidationError({"default_channels": gettext("Escolha os canais.")})
    allowed = allowed_channels(kind)
    unknown = sorted(set(default_channels) - set(allowed))
    if unknown:
        raise ValidationError({"default_channels": gettext("Canal não disponível para este aviso.")})
    return [channel for channel in allowed if channel in default_channels]


def set_instance_setting(
    *, actor: Any, type_code: str, enabled: bool, default_channels: list[str], expected_revision: int | None
) -> Any:
    """Create or change the platform's choice for ``type_code``."""
    from .models import InstanceNotificationSetting

    _require_platform(actor)
    kind = _kind(type_code)
    if type(enabled) is not bool:
        raise ValidationError({"enabled": gettext("Informe se o aviso é enviado.")})
    if kind.mandatory and not enabled:
        raise ValidationError(
            {"enabled": ValidationError(gettext("Um aviso obrigatório não pode ser desligado."), code="mandatory")}
        )
    channels = _channels(kind, default_channels)
    with transaction.atomic():
        record = InstanceNotificationSetting.objects.select_for_update().filter(type_code=kind.code).first()
        _check_revision(expected_revision, record)
        if record is None:
            try:
                with transaction.atomic():
                    record = InstanceNotificationSetting(
                        type_code=kind.code, enabled=enabled, default_channels=channels
                    )
                    record.save()
            except IntegrityError:
                raise ConcurrentUpdate("The instance notification setting was created concurrently.") from None
            return record
        record.enabled, record.default_channels = enabled, channels
        record.save(update_fields={"enabled", "default_channels"})
    return record


def clear_instance_setting(*, actor: Any, type_code: str, expected_revision: int) -> None:
    """Go back to the type's own defaults (logical deletion of the live row)."""
    from .models import InstanceNotificationSetting

    _require_platform(actor)
    kind = _kind(type_code)
    with transaction.atomic():
        record = InstanceNotificationSetting.objects.select_for_update().filter(type_code=kind.code).first()
        if record is None:
            raise ConcurrentUpdate("The instance notification setting is gone.")
        _check_revision(expected_revision, record)
        record.delete()


def _language(language: Any) -> str:
    code = str(language or "").lower()
    if code not in languages():
        raise ValidationError({"language": gettext("Idioma não disponível nesta instância.")})
    return code


def validate_text(kind: Any, title: Any, body: Any) -> tuple[str, str]:
    """Title and body checked against the placeholders of ``kind``; errors keyed by field."""
    from .registry import text_fields
    from .texts import validate

    allowed = text_fields(kind)
    errors: dict[str, Any] = {}
    clean: dict[str, str] = {}
    for name, value in (("title", title), ("body", body if body is not None else "")):
        try:
            clean[name] = validate(value, allowed=allowed, kind=name)
        except ValidationError as error:
            errors[name] = error
    if errors:
        raise ValidationError(errors)
    return clean["title"], clean["body"]


def _editable(kind: Any) -> None:
    if kind.fixed_text:
        raise ValidationError(
            {"title": ValidationError(gettext("O texto deste aviso é escrito em cada envio."), code="not_editable")}
        )


def set_instance_text(
    *, actor: Any, type_code: str, language: str, title: str, body: str, expected_revision: int | None
) -> Any:
    from .models import InstanceNotificationText

    _require_platform(actor)
    kind = _kind(type_code)
    _editable(kind)
    code = _language(language)
    title, body = validate_text(kind, title, body)
    with transaction.atomic():
        record = InstanceNotificationText.objects.select_for_update().filter(type_code=kind.code, language=code).first()
        _check_revision(expected_revision, record)
        if record is None:
            try:
                with transaction.atomic():
                    record = InstanceNotificationText(type_code=kind.code, language=code, title=title, body=body)
                    record.save()
            except IntegrityError:
                raise ConcurrentUpdate("The instance notification text was created concurrently.") from None
            return record
        record.title, record.body = title, body
        record.save(update_fields={"title", "body"})
    return record


def clear_instance_text(*, actor: Any, type_code: str, language: str, expected_revision: int) -> None:
    """Back to the factory text in ``language``."""
    from .models import InstanceNotificationText

    _require_platform(actor)
    kind = _kind(type_code)
    code = _language(language)
    with transaction.atomic():
        record = InstanceNotificationText.objects.select_for_update().filter(type_code=kind.code, language=code).first()
        if record is None:
            raise ConcurrentUpdate("The instance notification text is gone.")
        _check_revision(expected_revision, record)
        record.delete()
