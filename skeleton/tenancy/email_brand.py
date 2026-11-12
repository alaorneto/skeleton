"""Brand of the organization in emails (#173): name, colour and the logo as an inline part.

The logo travels inside the message (``Content-ID: <organization-logo>``, referenced as
``cid:organization-logo``), never as a public URL. Only the organization of the current context
is read, under its RLS, so one organization's logo never goes into another's email.
"""

from __future__ import annotations

import email.message
from typing import Any

from .branding import HEX, LOGO_MAX_BYTES, palette

LOGO_CID = "organization-logo"
DEFAULT_COLOR = "#1d4ed8"


def email_brand() -> dict[str, Any]:
    """``{name, color, logo}`` of the context organization; ``logo`` is ``(bytes, subtype)`` or None."""
    from skeleton.tenancy.context import current_context

    organization_id = current_context().organization_id
    if organization_id is None:
        return {"name": "", "color": DEFAULT_COLOR, "logo": None}
    from .models import Organization
    from .organization_settings import current_settings

    record = current_settings()
    organization = Organization.objects.filter(pk=organization_id).first()
    name = (record.display_name if record is not None and record.display_name else "") or (
        organization.name if organization is not None else ""
    )
    tokens = palette(record.brand_color) if record is not None else None
    color = tokens.brand if tokens is not None and HEX.fullmatch(tokens.brand) else DEFAULT_COLOR
    return {"name": name, "color": color, "logo": _logo(record)}


def _logo(record: Any) -> tuple[bytes, str] | None:
    if record is None or not record.logo_key:
        return None
    from .branding import open_logo

    found = open_logo()
    if found is None:
        return None
    from skeleton.files.storage import StorageError, open_object

    try:
        with open_object(record.logo_key) as stream:
            content = stream.read(LOGO_MAX_BYTES + 1)
    except (StorageError, FileNotFoundError, OSError):
        return None
    if len(content) > LOGO_MAX_BYTES:
        return None
    return content, found[1].split("/", 1)[1]


def template_context(brand: dict[str, Any]) -> dict[str, Any]:
    """What the email templates see: never the bytes, only whether the inline part exists."""
    return {
        "brand_name": brand["name"],
        "brand_color": brand["color"],
        "brand_logo_cid": LOGO_CID if brand["logo"] else "",
    }


def attach_logo(message: Any, brand: dict[str, Any]) -> None:
    """Add the logo as an inline image part referenced by ``cid:organization-logo``."""
    if not brand["logo"]:
        return
    content, subtype = brand["logo"]
    part = email.message.MIMEPart()
    part.set_content(content, maintype="image", subtype=subtype, disposition="inline", cid=f"<{LOGO_CID}>")
    message.attach(part)
