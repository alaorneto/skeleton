"""Settings of the current organization (#167): read by members, changed by the administrator.

The row is created on the first change. Until then the organization has the virtual
revision 0, so the first writer must send ``expected_revision=0`` and a concurrent first
write loses with ``ConcurrentUpdate``, like the app configuration. Every change runs under
``organization_lock`` (administrator only) and is audited by the table triggers.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context

# What the administrator writes; ``logo_key`` changes only through the logo upload (#168).
WRITE_FIELDS = ("display_name", "default_language", "default_timezone", "brand_color")
# The business calendar (#416), written as text and stored parsed.
CALENDAR_FIELDS = ("business_hours", "holidays")
# The organization's own address (#480): changed only when present, so other writers keep it.
SITE_FIELDS = ("site_enabled",)


_MISSING = object()


def settings_cache_key(organization_id: Any) -> tuple[str, str]:
    return ("skeleton.organization_settings", str(organization_id))


def current_settings() -> Any:
    """The settings row of the context organization, or ``None`` while it was never written.

    Memoized for the request (``request_cache``): the locale, the MFA gate (#740) and the shell
    read the same row. ``OrganizationSettings.save`` drops the memo, so a write is seen at once.
    """
    from .context import request_cache
    from .models import OrganizationSettings

    organization_id = current_context().organization_id
    cache = request_cache()
    key = settings_cache_key(organization_id)
    if cache is not None and key in cache:
        value = cache[key]
        return None if value is _MISSING else value
    record = OrganizationSettings.objects.filter(organization_id=organization_id).first()
    if cache is not None:
        cache[key] = _MISSING if record is None else record
    return record


def revision_of(settings: Any) -> int:
    return settings.revision if settings is not None else 0


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    unknown = set(data) - set(WRITE_FIELDS) - set(CALENDAR_FIELDS) - set(SITE_FIELDS)
    if unknown:
        raise ValidationError({name: gettext("Campo não permitido.") for name in sorted(unknown)})
    values: dict[str, Any] = {}
    if "business_hours" in data or "holidays" in data:
        from .business_hours import parse_holidays, parse_hours

        values["business_hours"] = parse_hours(data.get("business_hours", ""))
        values["holidays"] = parse_holidays(data.get("holidays", ""))
    for name in WRITE_FIELDS:
        value = data.get(name, "")
        if not isinstance(value, str):
            raise ValidationError({name: gettext("Use um texto.")})
        values[name] = value.strip()
    if "site_enabled" in data:
        if not isinstance(data["site_enabled"], bool):
            raise ValidationError({"site_enabled": gettext("Use verdadeiro ou falso.")})
        values["site_enabled"] = data["site_enabled"]
    values["brand_color"] = values["brand_color"].lower()
    from .branding import validate_brand_color

    validate_brand_color(values["brand_color"])
    return values


def clean_field(name: str, value: Any) -> Any:
    """One setting validated and normalized like ``update_settings`` stores it (#701)."""
    if name in CALENDAR_FIELDS:
        from .business_hours import parse_holidays, parse_hours

        if not isinstance(value, str):
            raise ValidationError({"value": gettext("Use um texto.")})
        return parse_hours(value) if name == "business_hours" else parse_holidays(value)
    if name in SITE_FIELDS:
        if not isinstance(value, bool):
            raise ValidationError({"value": gettext("Use verdadeiro ou falso.")})
        return value
    if name not in WRITE_FIELDS:
        raise ValidationError({"value": gettext("Campo não permitido.")})
    if not isinstance(value, str):
        raise ValidationError({"value": gettext("Use um texto.")})
    value = value.strip()
    if name == "brand_color":
        from .branding import validate_brand_color

        value = value.lower()
        validate_brand_color(value)
    if name == "default_language" and value:
        from django.conf import settings

        if value not in dict(settings.LANGUAGES):
            raise ValidationError({"value": gettext("Idioma indisponível.")})
    if name == "default_timezone" and value:
        from zoneinfo import available_timezones

        if value not in available_timezones():
            raise ValidationError({"value": gettext("Fuso horário inválido.")})
    return value


def write_settings_field(name: str, value: Any) -> Any:
    """Write one already validated setting in the current organization, without an actor.

    For the platform locking it (#701), inside the system context of the organization: the
    same lock, row and site index as ``update_settings``.
    """
    from skeleton.access.services import organization_lock

    from .models import OrganizationSettings
    from .site import sync_site

    with organization_lock() as organization:
        record = OrganizationSettings.objects.select_for_update().filter(organization=organization).first()
        if name == "site_enabled" and value and not (record is not None and record.site_enabled):
            refusal = site_refusal(organization)
            if refusal:
                raise ValidationError({"value": refusal})
        if record is None:
            record = OrganizationSettings(organization=organization, **{name: value})
            record.save()
        elif getattr(record, name) != value:
            setattr(record, name, value)
            record.save(update_fields={name})
        else:
            return record
        sync_site(organization, record)
        return record


def site_refusal(organization: Any) -> str | None:
    """Why this organization cannot have its own address (#480), or ``None`` when it can."""
    from skeleton.common.hostnames import slug_problem

    from .site import base_domain

    if not base_domain():
        return gettext(
            "Esta organização não pode ter endereço próprio: esta instalação não oferece endereço próprio "
            "para organizações."
        )
    if slug_problem(organization.slug) is not None:
        return gettext(
            "Esta organização não pode ter endereço próprio: o identificador dela não pode ser usado como endereço."
        )
    return None


def update_settings(*, actor: Any, data: dict[str, Any], expected_revision: int) -> Any:
    """Replace the writable settings with CAS; return the saved row (unchanged values write nothing).

    The organization site index (``OrganizationSite``) follows in the same transaction and lock.
    """
    from skeleton.access.services import organization_lock

    from .models import OrganizationSettings
    from .site import sync_site

    if type(expected_revision) is not int:
        raise ValidationError({"revision": gettext("Informe a revisão.")})
    values = _clean(data)
    from .setting_locks import require_unlocked

    # A setting the platform locked (#701) keeps its value: a form sends it unchanged.
    require_unlocked(
        {f"organization.{name}": value for name, value in values.items()},
        field_of={f"organization.{name}": name for name in values},
    )
    with organization_lock(actor) as organization:
        record = OrganizationSettings.objects.select_for_update().filter(organization=organization).first()
        if revision_of(record) != expected_revision:
            raise ConcurrentUpdate("Organization settings changed.")
        if values.get("site_enabled") and not (record is not None and record.site_enabled):
            refusal = site_refusal(organization)
            if refusal:
                raise ValidationError({"site_enabled": refusal})
        if record is None:
            record = OrganizationSettings(organization=organization, **values)
            record.save()
            sync_site(organization, record)
            return record
        changed = {name for name, value in values.items() if getattr(record, name) != value}
        for name in changed:
            setattr(record, name, values[name])
        if changed:
            record.save(update_fields=changed)
            sync_site(organization, record)
        return record


def update_mfa_policy(*, actor: Any, policy: Any, expected_revision: int) -> Any:
    """Set who must enrol MFA in the context organization (#740), with CAS; return the saved row.

    Only the administrator (``organization_lock``). The policy only tightens the platform's
    rules. Turning it on demands that the actor already has TOTP, so the change never locks the
    administrator out of the page that made it. Kept apart from ``update_settings`` so the
    service API of the organization never changes the MFA policy.
    """
    from skeleton.access.services import organization_lock
    from skeleton.accounts.mfa import MFA_POLICIES, has_totp

    from .models import OrganizationSettings

    if type(expected_revision) is not int:
        raise ValidationError({"revision": gettext("Informe a revisão.")})
    if not isinstance(policy, str) or policy not in MFA_POLICIES:
        raise ValidationError({"mfa_policy": gettext("Escolha uma das opções.")})
    with organization_lock(actor) as organization:
        record = OrganizationSettings.objects.select_for_update().filter(organization=organization).first()
        if revision_of(record) != expected_revision:
            raise ConcurrentUpdate("Organization settings changed.")
        current = record.mfa_policy if record is not None else ""
        if policy == current:
            return record
        if policy and not has_totp(actor):
            raise ValidationError(
                {"mfa_policy": gettext("Configure a autenticação em dois fatores na sua conta antes de exigi-la.")}
            )
        if record is None:
            record = OrganizationSettings(organization=organization, mfa_policy=policy)
            record.save()
            return record
        record.mfa_policy = policy
        record.save(update_fields=["mfa_policy"])
        return record
