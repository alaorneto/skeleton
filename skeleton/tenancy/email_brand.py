"""Brand of the organization in emails (#173): name, colour and the logo as an inline part.

The logo travels inside the message (``Content-ID: <organization-logo>``, referenced as
``cid:organization-logo``), never as a public URL. Only the organization of the current context
is read, under its RLS, so one organization's logo never goes into another's email.

Without an organization in the context the brand of the instance applies (#600): its product
name, the colour of its active theme and its logo. An organization without a colour of its own
takes the colour of the instance theme, but never the instance logo.
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
        return _instance_brand()
    from .models import Organization
    from .organization_settings import current_settings

    record = current_settings()
    organization = Organization.objects.filter(pk=organization_id).first()
    name = (record.display_name if record is not None and record.display_name else "") or (
        organization.name if organization is not None else ""
    )
    tokens = palette(record.brand_color) if record is not None else None
    color = tokens.brand if tokens is not None and HEX.fullmatch(tokens.brand) else _instance_color()
    return {"name": name, "color": color, "logo": _logo(record)}


def _instance_color() -> str:
    from skeleton.appearance.snapshot import current_appearance

    color = current_appearance().brand_color
    return color if color and HEX.fullmatch(color) else DEFAULT_COLOR


def _instance_brand() -> dict[str, Any]:
    """Name, theme colour and logo of the instance (#600); the defaults of the core without a brand."""
    from skeleton.appearance.images import open_image
    from skeleton.appearance.snapshot import current_appearance

    appearance = current_appearance()
    logo = None
    if appearance.logo_version:
        found = open_image("logo")
        if found is not None:
            logo = (found[0], found[1].split("/", 1)[1])
    return {"name": appearance.product_name, "color": _instance_color(), "logo": logo}


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
