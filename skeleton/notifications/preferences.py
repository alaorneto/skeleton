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
from django.utils.translation import gettext

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


class PreferenceNotFound(LookupError):
    """The person has no live row for this type and channel (the view answers 404, #278)."""


def type_default(kind: NotificationType, channel: str) -> str:
    return "immediate" if channel in kind.default_channels else "off"


def resolve(
    kind: NotificationType,
    channel: str,
    rows: dict[tuple[str, str], Any],
    instance_channels: tuple[str, ...] | None = None,
) -> Effective:
    """``rows`` maps ``(type_code, channel)`` to the person's live rows.

    ``instance_channels`` are the default channels the platform chose for the type (#784); they
    come after the person's own rows and before the type's defaults.
    """
    if channel == "in_app" and kind.mandatory:
        return Effective("immediate", DEFAULT_DIGEST_HOUR, "mandatory")
    own = rows.get((kind.code, channel))
    if own is not None:
        return Effective(own.mode, own.digest_hour, "own")
    default = rows.get((DEFAULT_TYPE, channel))
    if default is not None:
        return Effective(default.mode, default.digest_hour, "default")
    if instance_channels is not None:
        return Effective("immediate" if channel in instance_channels else "off", DEFAULT_DIGEST_HOUR, "instance")
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
    from .instance_settings import instance_channels

    return resolve(get_notification_type(type_code), channel, own_rows(user), instance_channels(type_code))


def wants(
    user_id: UUID,
    kind: NotificationType,
    channel: str,
    rows: dict[UUID, dict[tuple[str, str], Any]],
    instance_channels: tuple[str, ...] | None = None,
) -> str:
    """The resolved mode of ``channel`` for one person, from preloaded rows."""
    return resolve(kind, channel, rows.get(user_id, {}), instance_channels).mode


@dataclass(frozen=True)
class PreferenceLine:
    """One type (or the ``*`` default) with the resolved state of every channel."""

    type_code: str
    label: str
    mandatory: bool
    channels: dict[str, tuple[Effective, Any]]  # channel -> (effective, live row or None)
    kind: Any = None
    # Channels this type never uses: push of the platform's own notices (#787).
    unavailable: tuple[str, ...] = ()


def _apps_of_people(user: Any) -> frozenset[str] | None:
    """Apps active in at least one organization of the person, from the instance summary (#790).

    ``None`` when some organization has no summary yet: every installed app then counts, so a
    notice is never hidden by a summary that did not run.
    """
    from skeleton.insights.models import OrganizationSnapshot
    from skeleton.tenancy.context import memberships_for_user

    organizations = {row["organization_id"] for row in memberships_for_user(user, all_organizations=True)}
    if not organizations:
        return frozenset()
    snapshots = dict(
        OrganizationSnapshot.objects.filter(subject_organization_id__in=organizations).values_list(
            "subject_organization_id", "apps_effective"
        )
    )
    if set(snapshots) != organizations:
        return None
    return frozenset(label for labels in snapshots.values() for label in labels or ())


def available_kinds(user: Any) -> list[NotificationType]:
    """The types this person can receive (#790), in code order.

    Members' types turned on in the instance (or not turned off) and owned by the core or by an app
    active in one of the person's organizations; the platform's own notices only for its
    administrators and, with ``platform_capability``, its holders; never the external people's.
    """
    from skeleton.accounts.platform import is_platform_admin, platform_capabilities

    from .instance_settings import instance_state
    from .registry import is_core_owner

    instance = instance_state()
    admin = is_platform_admin(user)
    capabilities = platform_capabilities(user)
    apps = _apps_of_people(user)
    result = []
    for kind in all_notification_types():
        state = instance.get(kind.code)
        if not kind.mandatory and state is not None and not state.enabled:
            continue
        if kind.audience == "external":
            continue
        if kind.audience == "platform":
            if admin or (kind.platform_capability and kind.platform_capability in capabilities):
                result.append(kind)
            continue
        if kind.owner_app and not is_core_owner(kind.owner_app) and apps is not None and kind.owner_app not in apps:
            continue
        result.append(kind)
    return result


def preference_lines(user: Any) -> list[PreferenceLine]:
    from .instance_settings import instance_state

    rows = own_rows(user)
    instance = instance_state()
    defaults = NotificationType(DEFAULT_TYPE, gettext("Padrão para todos os tipos"), default_channels=("in_app",))
    lines = []
    for kind in (defaults, *available_kinds(user)):
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
                chosen = instance.get(kind.code)
                state = resolve(kind, channel, rows, chosen.default_channels if chosen else None)
            channels[channel] = (state, row)
        unavailable = ("push",) if kind.audience == "platform" else ()
        lines.append(PreferenceLine(kind.code, kind.label, kind.mandatory, channels, kind, unavailable))
    return lines


def _validate(type_code: Any, channel: Any, mode: Any, digest_hour: Any) -> int:
    if not isinstance(type_code, str) or (type_code != DEFAULT_TYPE and known_type(type_code) is None):
        raise ValidationError({"type_code": gettext("Tipo de notificação desconhecido.")})
    if channel not in CHANNELS:
        raise ValidationError({"channel": gettext("Canal desconhecido.")})
    if mode not in MODES[channel]:
        raise ValidationError({"mode": gettext("Modo indisponível para este canal.")})
    kind = known_type(type_code)
    if kind is not None and kind.mandatory and channel == "in_app" and mode == "off":
        raise ValidationError({"mode": gettext("Este tipo é obrigatório na caixa de notificações.")})
    if digest_hour is None:
        return DEFAULT_DIGEST_HOUR
    if type(digest_hour) is not int or not 0 <= digest_hour <= 23:
        raise ValidationError({"digest_hour": gettext("Use uma hora de 0 a 23.")})
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
    kind = known_type(type_code)
    if kind is not None and kind.audience == "platform" and channel == "push" and mode != "off":
        raise ValidationError(gettext("Os avisos da plataforma não usam push."))  # #787
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
        raise PreferenceNotFound
    if expected_revision != row.revision:
        raise ConcurrentUpdate("The preference was changed by another operation.")
    row.delete()
