"""Settings of the current organization (#167): read by members, changed by the administrator.

The row is created on the first change. Until then the organization has the virtual
revision 0, so the first writer must send ``expected_revision=0`` and a concurrent first
write loses with ``ConcurrentUpdate``, like the app configuration. Every change runs under
``organization_lock`` (administrator only) and is audited by the table triggers.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context

# What the administrator writes; ``logo_key`` changes only through the logo upload (#168).
WRITE_FIELDS = ("display_name", "default_language", "default_timezone", "brand_color")


def current_settings() -> Any:
    """The settings row of the context organization, or ``None`` while it was never written."""
    from .models import OrganizationSettings

    return OrganizationSettings.objects.filter(organization_id=current_context().organization_id).first()


def revision_of(settings: Any) -> int:
    return settings.revision if settings is not None else 0


def _clean(data: dict[str, Any]) -> dict[str, str]:
    unknown = set(data) - set(WRITE_FIELDS)
    if unknown:
        raise ValidationError({name: "Campo não permitido." for name in sorted(unknown)})
    values = {}
    for name in WRITE_FIELDS:
        value = data.get(name, "")
        if not isinstance(value, str):
            raise ValidationError({name: "Use um texto."})
        values[name] = value.strip()
    values["brand_color"] = values["brand_color"].lower()
    from .branding import validate_brand_color

    validate_brand_color(values["brand_color"])
    return values


def update_settings(*, actor: Any, data: dict[str, Any], expected_revision: int) -> Any:
    """Replace the writable settings with CAS; return the saved row (unchanged values write nothing)."""
    from skeleton.access.services import organization_lock

    from .models import OrganizationSettings

    if type(expected_revision) is not int:
        raise ValidationError({"revision": "Informe a revisão."})
    values = _clean(data)
    with organization_lock(actor) as organization:
        record = OrganizationSettings.objects.select_for_update().filter(organization=organization).first()
        if revision_of(record) != expected_revision:
            raise ConcurrentUpdate("Organization settings changed.")
        if record is None:
            record = OrganizationSettings(organization=organization, **values)
            record.save()
            return record
        changed = {name for name, value in values.items() if getattr(record, name) != value}
        for name in changed:
            setattr(record, name, values[name])
        if changed:
            record.save(update_fields=changed)
        return record
