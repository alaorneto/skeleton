"""Notification types are registered in code by the app that owns them.

Every type says where it comes from (``context``, #782): ``platform`` (the platform's own notices
to its administrators, its announcements and what the platform tells an organization, such as
billing), ``organization`` (what happens inside the organization, such as a mention or a new
member) or ``app`` (an installed app). A type of an app that does not declare it is ``app``.
A type may also carry a factory text (``title`` and ``body``, with ``{field}`` placeholders,
:mod:`skeleton.notifications.texts`) that the platform and the organization may replace.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CONTEXTS = ("platform", "organization", "app")
AUDIENCES = ("members", "external", "platform")
FIELD_KINDS = ("text", "number", "date", "datetime", "money", "person")
PLATFORM_CHANNELS = ("in_app", "email")


@dataclass(frozen=True)
class NotificationField:
    """A placeholder of the text: its label in the editor and how its value is formatted.

    ``kind`` is ``text``, ``number``, ``date``, ``datetime``, ``money`` (an integer in the minor
    unit, with the currency read from ``currency_field``) or ``person`` (a user UUID of the
    payload, shown by the display name resolved at fan-out).
    """

    name: str
    label: Any
    kind: str = "text"
    currency_field: str = ""


@dataclass(frozen=True)
class NotificationType:
    """A kind of notification.

    ``payload_fields`` are the scalar keys ``notify()`` accepts; ``snapshot_fields``
    are copied from the target record and must be ``read_fields`` and never
    ``sensitive_fields`` of ``target_table_key``. ``group_key`` is a format string
    over the payload and ``target`` (for example ``"task:{target}"``): unread
    notifications of a person with the same key collapse into one line.
    """

    code: str
    label: Any
    payload_fields: tuple[str, ...] = ()
    snapshot_fields: tuple[str, ...] = ()
    target_table_key: str = ""
    default_channels: tuple[str, ...] = ("in_app",)
    mandatory: bool = False
    group_key: str = ""
    owner_app: str = ""
    # Web push carries only the type label and a link; True also sends the rendered title (#56).
    push_snapshot: bool = False
    # For a type without a target table: ``open_url(user, row) -> str | None`` re-authorizes the
    # person and returns where the notification leads (the flow task page, #204). For a target of a
    # derived table (#562), which has no generic page, it is the only link, given after the core
    # re-authorized the read of the target; without it the notification opens nowhere.
    open_url: Callable[[Any, Any], str | None] | None = None
    # For a type without a target table: ``authorize(user, notification) -> bool`` re-checks
    # each person at fan-out time, so access revoked since the notification was created
    # stops the delivery (comment mentions, #188).
    authorize: Callable[[Any, Any], bool] | None = None
    # ``"external"`` (#535): the recipients are external people of the portal (active ExternalAccess),
    # named one by one, reached only by email with a link to the portal; no inbox row, no target.
    # ``"platform"`` (#787): the platform's administrators (and holders of ``platform_capability``),
    # without an organization.
    audience: str = "members"
    # Where the type comes from (#782): ``platform``, ``organization`` or ``app``; empty means
    # ``app`` for a type owned by an app (a core type declares it).
    context: str = ""
    # One sentence for the configuration screens ("When someone joins the organization").
    description: Any = ""
    # The placeholders of the texts, with their labels and formats.
    fields: tuple[NotificationField, ...] = ()
    # The factory text (lazy translations) with ``{field}`` placeholders; empty keeps the label.
    title: Any = ""
    body: Any = ""
    # ``False``: the organization does not rewrite the text of this app or organization type.
    org_editable: bool = True
    # ``True``: the text is the content of each notification (an announcement, #789): nobody edits it.
    fixed_text: bool = False
    # With ``audience="platform"``: holders of this platform capability also receive it.
    platform_capability: str = ""


_types: dict[str, NotificationType] = {}
_duplicates: list[str] = []


def register_notification_type(notification_type: NotificationType) -> NotificationType:
    existing = _types.get(notification_type.code)
    if existing is not None and existing != notification_type:
        _duplicates.append(notification_type.code)
        return existing
    _types[notification_type.code] = notification_type
    return notification_type


def get_notification_type(code: str) -> NotificationType:
    return _types[code]


def all_notification_types() -> tuple[NotificationType, ...]:
    return tuple(_types[code] for code in sorted(_types))


def is_core_owner(owner_app: str) -> bool:
    return owner_app.startswith("sk_")


def type_context(kind: NotificationType) -> str:
    """The declared context, or ``app`` for an app type that does not declare it."""
    if kind.context:
        return kind.context
    return "app" if kind.owner_app and not is_core_owner(kind.owner_app) else ""


def org_can_edit_text(kind: NotificationType) -> bool:
    """Whether the organization may write its own text for this type (#785)."""
    return type_context(kind) in ("organization", "app") and kind.org_editable and not kind.fixed_text


def org_can_disable(kind: NotificationType) -> bool:
    """Whether the organization may turn this type off for every member (#785)."""
    return type_context(kind) in ("organization", "app") and not kind.mandatory


def text_fields(kind: NotificationType) -> frozenset[str]:
    """Every placeholder a text of ``kind`` may use: its fields and the fixed ones."""
    from .texts import FIXED

    return frozenset(field.name for field in kind.fields) | frozenset(FIXED)


def _audience_problems(kind: NotificationType) -> list[str]:
    problems = []
    if kind.audience not in AUDIENCES:
        problems.append(f"{kind.code}: audience is members, external or platform")
    if kind.audience == "external" and (kind.target_table_key or tuple(kind.default_channels) != ("email",)):
        problems.append(f"{kind.code}: an external type has no target and only the email channel")
    if kind.audience == "platform":
        if type_context(kind) != "platform":
            problems.append(f"{kind.code}: a platform audience needs the platform context")
        if kind.target_table_key or kind.snapshot_fields:
            problems.append(f"{kind.code}: a platform type has no target")
        if not set(kind.default_channels) <= set(PLATFORM_CHANNELS):
            problems.append(f"{kind.code}: a platform type uses only the in_app and email channels")
    if kind.platform_capability:
        from skeleton.accounts.platform import PLATFORM_CAPABILITIES

        if kind.audience != "platform":
            problems.append(f"{kind.code}: platform_capability needs the platform audience")
        elif kind.platform_capability not in PLATFORM_CAPABILITIES:
            problems.append(f"{kind.code}: unknown platform capability {kind.platform_capability}")
    return problems


def _context_problems(kind: NotificationType) -> list[str]:
    problems = []
    if kind.context not in ("", *CONTEXTS):
        problems.append(f"{kind.code}: context is platform, organization or app")
    elif is_core_owner(kind.owner_app) and not kind.context:
        problems.append(f"{kind.code}: a core type declares its context")
    elif kind.owner_app and not is_core_owner(kind.owner_app) and kind.context in ("platform", "organization"):
        problems.append(f"{kind.code}: an app type has the app context")
    if kind.fixed_text and type_context(kind) != "platform":
        problems.append(f"{kind.code}: a fixed text belongs to a platform type")
    return problems


def _field_problems(kind: NotificationType) -> list[str]:
    problems = []
    declared = set(kind.payload_fields) | set(kind.snapshot_fields)
    names = [field.name for field in kind.fields]
    if len(names) != len(set(names)):
        problems.append(f"{kind.code}: duplicate text field")
    for field in kind.fields:
        if field.name not in declared:
            problems.append(f"{kind.code}: text field {field.name} is not a payload or snapshot field")
        if field.kind not in FIELD_KINDS:
            problems.append(f"{kind.code}: text field {field.name} has an unknown kind {field.kind}")
        if field.kind == "money" and field.currency_field not in declared:
            problems.append(f"{kind.code}: money field {field.name} needs a declared currency_field")
        if field.kind == "person" and field.name not in kind.payload_fields:
            problems.append(f"{kind.code}: person field {field.name} must be a payload field")
    return problems


def _text_problems(kind: NotificationType) -> list[str]:
    from django.core.exceptions import ValidationError
    from django.utils import translation

    from .texts import validate

    problems = []
    with translation.override("pt-br"):
        title, body = str(kind.title or ""), str(kind.body or "")
    if body and not title:
        problems.append(f"{kind.code}: a factory body needs a title")
    allowed = text_fields(kind)
    for part, text in (("title", title), ("body", body)):
        if not text:
            continue
        try:
            validate(text, allowed=allowed, kind=part)
        except ValidationError as error:
            problems.append(f"{kind.code}: factory {part} is invalid ({', '.join(error.messages)})")
    return problems


def notification_problems() -> list[str]:
    from skeleton.system.registry import get_spec

    problems = [f"{code}: duplicate notification type" for code in sorted(set(_duplicates))]
    for kind in all_notification_types():
        problems += _audience_problems(kind)
        problems += _context_problems(kind)
        problems += _field_problems(kind)
        problems += _text_problems(kind)
        if not kind.target_table_key:
            if kind.snapshot_fields:
                problems.append(f"{kind.code}: snapshot fields need a target table")
            continue
        try:
            spec = get_spec(kind.target_table_key)
        except KeyError:
            problems.append(f"{kind.code}: unknown target table {kind.target_table_key}")
            continue
        for name in kind.snapshot_fields:
            if name in spec.sensitive_fields:
                problems.append(f"{kind.code}: snapshot field {name} is sensitive")
            elif name not in spec.read_fields:
                problems.append(f"{kind.code}: snapshot field {name} is not a read field")
        for name in kind.payload_fields:
            if name in spec.sensitive_fields:
                problems.append(f"{kind.code}: payload field {name} is sensitive")
    return problems
