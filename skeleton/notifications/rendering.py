"""The text a person receives (#783): chosen, formatted and rendered at fan-out, then stored.

The fan-out runs in the system context of the organization: it is the only moment that may read
the organization's own texts (the global inbox has no tenant) and the names of the people a
notification is about. The rendered title and body are stored on the person's row, so a text
edited later never rewrites what was delivered.

The text is the first that exists, in the person's language: the organization's (#785), the
instance's (#784), the type's factory text, or the old presentation (the label and the values of
the snapshot).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from .texts import BODY_MAX, RECIPIENT, TITLE_MAX, first_name, format_value, placeholders, render

ORGANIZATION = "organization"
INSTANCE = "instance"
TYPE = "type"


@dataclass(frozen=True)
class TextTemplate:
    title: str
    body: str
    source: str  # "organization", "instance" or "type"

    def names_recipient(self) -> bool:
        """Whether the text uses a placeholder of the person who receives it."""
        return bool((placeholders(self.title) | placeholders(self.body)) & set(RECIPIENT))


@dataclass(frozen=True)
class Rendered:
    title: str
    body: str
    subject_people: tuple[str, ...] = ()


def _base(language: str) -> str:
    return language.lower().split("-")[0]


def _pick(texts: Mapping[str, tuple[str, str]], language: str) -> tuple[str, str] | None:
    """The text of ``language``, else of its base language (``pt-br`` → ``pt``), else of any variant."""
    language = language.lower()
    if language in texts:
        return texts[language]
    base = _base(language)
    for code, value in texts.items():
        if code == base or _base(code) == base:
            return value
    return None


@dataclass(frozen=True)
class TextResolver:
    """Every text of one type a fan-out may use, loaded once: ``{language: (title, body)}`` per source."""

    kind: Any
    organization: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    instance: Mapping[str, tuple[str, str]] = field(default_factory=dict)

    def template(self, language: str) -> TextTemplate | None:
        """The text for ``language``; call it inside ``translation.override`` for the factory text."""
        from .registry import org_can_edit_text

        if org_can_edit_text(self.kind):
            chosen = _pick(self.organization, language)
            if chosen is not None:
                return TextTemplate(chosen[0], chosen[1], ORGANIZATION)
        if not self.kind.fixed_text:
            chosen = _pick(self.instance, language)
            if chosen is not None:
                return TextTemplate(chosen[0], chosen[1], INSTANCE)
        title = str(self.kind.title or "")
        if title:
            return TextTemplate(title, str(self.kind.body or ""), TYPE)
        return None


def resolver_for(kind: Any, *, organization_texts: bool = True) -> TextResolver:
    """The texts of ``kind`` from the instance and, inside a tenant, from the current organization."""
    from .instance_settings import instance_texts
    from .organization_settings import organization_texts as texts_of_organization
    from .registry import org_can_edit_text

    organization = texts_of_organization(kind.code) if organization_texts and org_can_edit_text(kind) else {}
    return TextResolver(kind, organization=organization, instance=instance_texts(kind.code))


def person_codes(kind: Any, payload: Mapping[str, Any]) -> tuple[str, ...]:
    """The UUIDs (as text) named by the ``person`` fields of ``payload``."""
    names = [item.name for item in kind.fields if item.kind == "person"]
    return tuple(dict.fromkeys(str(payload[name]) for name in names if payload.get(name)))


def display_name(user: Any) -> str:
    """The name the screens show: the profile's display name, else first and last name."""
    profile = getattr(user, "profile", None)
    name = (getattr(profile, "display_name", "") or "").strip()
    return name or f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()


def people_names(user_ids: Iterable[str]) -> dict[str, str]:
    """Display names of people who are or were members of the current organization; nobody else."""
    from skeleton.tenancy.context import current_context
    from skeleton.tenancy.models import Membership

    ids = []
    for value in user_ids:
        try:
            ids.append(UUID(str(value)))
        except ValueError:
            continue
    organization_id = current_context().organization_id
    if not ids or organization_id is None:
        return {}
    names: dict[str, str] = {}
    rows = Membership.all_objects.filter(organization_id=organization_id, user_id__in=ids).select_related(
        "user", "user__profile"
    )
    for membership in rows:
        user = membership.user
        name = display_name(user)
        if name:
            names[str(user.pk)] = name
    return names


def render_notification(
    kind: Any,
    *,
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    organization_name: str,
    people: Mapping[str, str],
    resolver: TextResolver,
    language: str,
    recipient_name: str = "",
    sent_at: datetime | None = None,
    template: TextTemplate | None = None,
) -> Rendered:
    """Title and body for one language; call it inside the person's language and time zone."""
    template = template or resolver.template(language)
    subjects = person_codes(kind, payload)
    if template is None:
        title = str(kind.label)
        body = " · ".join(str(value) for value in snapshot.values() if value not in (None, ""))
        return Rendered(title[:TITLE_MAX], body[:BODY_MAX], subjects)
    raw = {**dict(snapshot), **dict(payload)}
    values: dict[str, Any] = {
        item.name: format_value(item, raw.get(item.name), values=raw, people=people) for item in kind.fields
    }
    values.update(fixed_values(kind, organization_name=organization_name, sent_at=sent_at))
    values["destinatario"] = recipient_name
    values["destinatario_primeiro_nome"] = first_name(recipient_name)
    title = " ".join(render(template.title, values).split())
    body = render(template.body, values).strip()
    return Rendered(title[:TITLE_MAX] or str(kind.label)[:TITLE_MAX], body[:BODY_MAX], subjects)


def fixed_values(kind: Any, *, organization_name: str, sent_at: datetime | None = None) -> dict[str, str]:
    """The fixed placeholders but the recipient's; call it inside the person's language and time zone."""
    from django.utils import timezone

    from .texts import date_text, product_name

    return {
        "organizacao": organization_name,
        "aviso": str(kind.label),
        "plataforma": product_name(),
        "data_atual": date_text(sent_at or timezone.now()),
    }


def recipient_locales(users: Iterable[Any]) -> dict[Any, tuple[str, str]]:
    """``{user_id: (language, time zone)}`` with the order of the screens: profile, organization, instance.

    ``users`` come with ``profile`` loaded; the organization and instance defaults are read once.
    """
    from skeleton.common.locale import (
        instance_preferences,
        organization_preferences,
        supported_language,
        valid_timezone,
    )

    org_language, org_zone = organization_preferences()
    fallback = instance_preferences()
    result = {}
    for user in users:
        profile = getattr(user, "profile", None)
        language = supported_language(getattr(profile, "language", None)) or org_language or fallback.language
        zone = valid_timezone(getattr(profile, "timezone", None)) or org_zone or fallback.timezone
        result[user.pk] = (language, zone)
    return result


def render_for_people(
    kind: Any,
    *,
    payload: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    users: Iterable[Any],
    organization_name: str,
    resolver: TextResolver | None = None,
    sent_at: datetime | None = None,
) -> dict[Any, Rendered]:
    """One rendering per distinct (language, time zone), shared by everyone with that pair.

    A text that names the person who receives it is rendered for each person instead (#806).
    ``sent_at`` (the creation of the notification) is the ``{data_atual}`` of every batch.
    """
    from django.utils import timezone, translation

    resolver = resolver or resolver_for(kind)
    users = list(users)
    names = {user.pk: display_name(user) for user in users}
    locales = recipient_locales(users)
    people = people_names(person_codes(kind, payload))
    templates: dict[str, TextTemplate | None] = {}
    cache: dict[tuple[str, str, str], Rendered] = {}
    result = {}
    for user_id, pair in locales.items():
        language, zone = pair
        with translation.override(language), timezone.override(zone):
            if language not in templates:
                templates[language] = resolver.template(language)
            template = templates[language]
            name = names.get(user_id, "") if template is not None and template.names_recipient() else ""
            key = (language, zone, name)
            if key not in cache:
                cache[key] = render_notification(
                    kind,
                    payload=payload,
                    snapshot=snapshot,
                    organization_name=organization_name,
                    people=people,
                    resolver=resolver,
                    language=language,
                    recipient_name=name,
                    sent_at=sent_at,
                    template=template,
                )
        result[user_id] = cache[key]
    return result


def organization_name() -> str:
    """The public name of the context organization, as the email brand shows it."""
    from skeleton.tenancy.email_brand import email_brand

    try:
        return str(email_brand().get("name") or "")
    except Exception:  # noqa: BLE001 - a missing brand leaves the placeholder empty, never a failed fan-out
        return ""
