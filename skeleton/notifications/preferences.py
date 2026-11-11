"""Notification preferences per type and channel (#53).

Resolution, from the most specific: the person's row for the type, their ``*`` row (default
for every type), then the type's ``default_channels``. A mandatory type always reaches the
in-app inbox, whatever the rows say. A person reads and changes only their own rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction

from skeleton.common.exceptions import ConcurrentUpdate

from .models import DEFAULT_TYPE, UserNotificationPreference
from .registry import NotificationType, all_notification_types, get_notification_type

CHANNELS = ("in_app", "email", "push")
MODES = {
    "in_app": ("off", "immediate"),
    "email": ("off", "immediate", "daily", "weekly"),
    "push": ("off", "immediate"),
}
DIGEST_MODES = ("daily", "weekly")
MODE_LABELS = tuple(UserNotificationPreference.Mode.choices)
CHANNEL_LABELS = tuple(UserNotificationPreference.Channel.choices)
DEFAULT_DIGEST_HOUR = 8


@dataclass(frozen=True)
class Effective:
    """The mode that applies to one type and channel, and where it came from.

    ``source`` is ``type`` (no row: the type's defaults), ``default`` (the ``*`` row),
    ``own`` (the row of the type) or ``mandatory`` (in-app of a mandatory type).
    """

    mode: str
    digest_hour: int
    source: str


def type_default(kind: NotificationType, channel: str) -> str:
    return "immediate" if channel in kind.default_channels else "off"


def resolve(kind: NotificationType, channel: str, rows: dict[tuple[str, str], Any]) -> Effective:
    """``rows`` maps ``(type_code, channel)`` to the person's live rows."""
    if channel == "in_app" and kind.mandatory:
        return Effective("immediate", DEFAULT_DIGEST_HOUR, "mandatory")
    own = rows.get((kind.code, channel))
    if own is not None:
        return Effective(own.mode, own.digest_hour, "own")
    default = rows.get((DEFAULT_TYPE, channel))
    if default is not None:
        return Effective(default.mode, default.digest_hour, "default")
    return Effective(type_default(kind, channel), DEFAULT_DIGEST_HOUR, "type")


def rows_by_user(user_ids: Iterable[UUID], *, using: str = "default") -> dict[UUID, dict[tuple[str, str], Any]]:
    """Live rows of many people at once (fan-out and digests), keyed by user."""
    result: dict[UUID, dict[tuple[str, str], Any]] = {}
    ids = list(user_ids)
    if not ids:
        return result
    for row in UserNotificationPreference.objects.using(using).filter(user_id__in=ids):
        result.setdefault(row.user_id, {})[(row.type_code, row.channel)] = row
    return result


def own_rows(user: Any) -> dict[tuple[str, str], Any]:
    return rows_by_user([user.pk]).get(user.pk, {})


def effective(user: Any, type_code: str, channel: str) -> Effective:
    return resolve(get_notification_type(type_code), channel, own_rows(user))


def wants(user_id: UUID, kind: NotificationType, channel: str, rows: dict[UUID, dict[tuple[str, str], Any]]) -> str:
    """The resolved mode of ``channel`` for one person, from preloaded rows."""
    return resolve(kind, channel, rows.get(user_id, {})).mode


@dataclass(frozen=True)
class PreferenceLine:
    """One type (or the ``*`` default) with the resolved state of every channel."""

    type_code: str
    label: str
    mandatory: bool
    channels: dict[str, tuple[Effective, Any]]  # channel -> (effective, live row or None)


def preference_lines(user: Any) -> list[PreferenceLine]:
    rows = own_rows(user)
    defaults = NotificationType(DEFAULT_TYPE, "Padrão para todos os tipos", default_channels=("in_app",))
    lines = []
    for kind in (defaults, *all_notification_types()):
        channels = {}
        for channel in CHANNELS:
            row = rows.get((kind.code, channel))
            if kind.code == DEFAULT_TYPE:
                state = (
                    Effective(row.mode, row.digest_hour, "own")
                    if row is not None
                    else Effective(type_default(kind, channel), DEFAULT_DIGEST_HOUR, "type")
                )
            else:
                state = resolve(kind, channel, rows)
            channels[channel] = (state, row)
        lines.append(PreferenceLine(kind.code, kind.label, kind.mandatory, channels))
    return lines


def _validate(type_code: Any, channel: Any, mode: Any, digest_hour: Any) -> int:
    if not isinstance(type_code, str) or (type_code != DEFAULT_TYPE and known_type(type_code) is None):
        raise ValidationError({"type_code": "Tipo de notificação desconhecido."})
    if channel not in CHANNELS:
        raise ValidationError({"channel": "Canal desconhecido."})
    if mode not in MODES[channel]:
        raise ValidationError({"mode": "Modo indisponível para este canal."})
    kind = known_type(type_code)
    if kind is not None and kind.mandatory and channel == "in_app" and mode == "off":
        raise ValidationError({"mode": "Este tipo é obrigatório na caixa de notificações."})
    if digest_hour is None:
        return DEFAULT_DIGEST_HOUR
    if type(digest_hour) is not int or not 0 <= digest_hour <= 23:
        raise ValidationError({"digest_hour": "Use uma hora de 0 a 23."})
    return digest_hour


def known_type(type_code: str) -> NotificationType | None:
    try:
        return get_notification_type(type_code)
    except KeyError:
        return None


def _live(user: Any, type_code: str, channel: str) -> Any:
    return (
        UserNotificationPreference.objects.select_for_update()
        .filter(user_id=user.pk, type_code=type_code, channel=channel)
        .first()
    )


@transaction.atomic
def set_preference(
    user: Any,
    *,
    type_code: str,
    channel: str,
    mode: str,
    digest_hour: int | None = None,
    expected_revision: int | None,
) -> Any:
    """Create or change the person's own row with CAS.

    An existing row requires ``expected_revision`` equal to its revision; a new row requires
    ``expected_revision=None``. Anything else is ``ConcurrentUpdate``.
    """
    hour = _validate(type_code, channel, mode, digest_hour)
    row = _live(user, type_code, channel)
    if row is None:
        if expected_revision is not None:
            raise ConcurrentUpdate("The preference was removed by another operation.")
        row = UserNotificationPreference(
            user_id=user.pk, type_code=type_code, channel=channel, mode=mode, digest_hour=hour
        )
        row.save()
        return row
    if expected_revision != row.revision:
        raise ConcurrentUpdate("The preference was changed by another operation.")
    if (row.mode, row.digest_hour) == (mode, hour):
        return row
    row.mode, row.digest_hour = mode, hour
    row.save(update_fields={"mode", "digest_hour"})
    return row


@transaction.atomic
def clear_preference(user: Any, *, type_code: str, channel: str, expected_revision: int) -> None:
    """Remove the person's row (soft delete): the next level of the resolution applies again."""
    row = _live(user, type_code, channel)
    if row is None:
        from django.http import Http404

        raise Http404
    if expected_revision != row.revision:
        raise ConcurrentUpdate("The preference was changed by another operation.")
    row.delete()
