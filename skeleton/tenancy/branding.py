"""Brand of the organization (#168): colour palette with WCAG AA contrast and the logo object.

The administrator picks one primary colour. It must reach WCAG AA (4.5:1) against white,
because buttons draw white text on it and links draw it on the white canvas. The dark theme
uses a lighter variant for text, mixed towards white until it reaches 4.5:1 on the dark canvas,
so the contrast holds in both themes by construction. The CSS is built only from validated
``#rrggbb`` values (also pinned by a database check), never from free text.

The logo is a PNG, JPEG or WebP of at most 512 KB stored privately under
``org/<organization>/branding/<uuid>``. It is served inline only to members of the
organization, and replacing or removing it deletes the old object after the commit.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import IO, Any

from django.core.exceptions import ValidationError
from django.db import transaction

from skeleton.common.exceptions import ConcurrentUpdate

AA = 4.5
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
# The dark canvas of the theme (``--color-ink-950``, oklch(0.155 0.014 268)) in sRGB.
DARK_CANVAS = (13, 16, 24)
LOGO_MAX_BYTES = 512 * 1024
LOGO_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
HEX = re.compile(r"^#[0-9a-f]{6}$")


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(255.0, channel))):02x}" for channel in rgb)


def _luminance(rgb: tuple[float, float, float]) -> float:
    def linear(channel: float) -> float:
        value = channel / 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _mix(rgb: tuple[int, int, int], other: tuple[int, int, int], amount: float) -> tuple[float, float, float]:
    return tuple(channel + (target - channel) * amount for channel, target in zip(rgb, other, strict=True))  # type: ignore[return-value]


def validate_brand_color(color: str) -> None:
    """Refuse a colour that does not reach AA against white (button text and links)."""
    if not color:
        return
    if not HEX.fullmatch(color):
        raise ValidationError({"brand_color": "Use uma cor no formato #RRGGBB."})
    ratio = contrast(_rgb(color), WHITE)
    if ratio < AA:
        raise ValidationError(
            {
                "brand_color": ValidationError(
                    f"Contraste {ratio:.1f}:1 com o branco; use uma cor mais escura (mínimo 4,5:1).",
                    code="contrast",
                )
            }
        )


@dataclass(frozen=True, slots=True)
class Palette:
    brand: str
    brand_strong: str
    text_light: str
    text_light_strong: str
    text_dark: str
    text_dark_strong: str

    def css(self) -> str:
        light = (
            f"--color-brand:{self.brand};--color-brand-strong:{self.brand_strong};"
            f"--color-fg-brand:{self.text_light};--color-fg-brand-strong:{self.text_light_strong};"
        )
        dark = (
            f"--color-brand:{self.brand};--color-brand-strong:{self.brand_strong};"
            f"--color-fg-brand:{self.text_dark};--color-fg-brand-strong:{self.text_dark_strong};"
        )
        return f":root{{{light}}}@media (prefers-color-scheme: dark){{:root{{{dark}}}}}"


def palette(color: str) -> Palette | None:
    """The theme tokens of a validated colour, or None for the core theme."""
    if not color or not HEX.fullmatch(color) or contrast(_rgb(color), WHITE) < AA:
        return None
    rgb = _rgb(color)
    strong = _hex(_mix(rgb, BLACK, 0.18))
    dark_text = next(
        _hex(_mix(rgb, WHITE, step / 20))
        for step in range(0, 21)
        if contrast(_mix(rgb, WHITE, step / 20), DARK_CANVAS) >= AA
    )
    dark_strong = _hex(_mix(_rgb(dark_text), WHITE, 0.3))
    return Palette(color, strong, color, strong, dark_text, dark_strong)


def logo_key(organization_id: Any) -> str:
    return f"org/{uuid.UUID(str(organization_id))}/branding/{uuid.uuid4()}"


def _checked_logo(upload: Any) -> str:
    """The MIME type proven by the content and the name; size and type are both enforced."""
    from skeleton.files.sniffing import HEAD_BYTES, detect

    size = getattr(upload, "size", None)
    if size is None or size > LOGO_MAX_BYTES:
        raise ValidationError({"logo": "O logo pode ter até 512 KB."})
    head = upload.read(HEAD_BYTES)
    upload.seek(0)
    kind = detect(head, getattr(upload, "name", "") or "")
    if kind not in LOGO_TYPES:
        raise ValidationError({"logo": "Use uma imagem PNG, JPEG ou WebP."})
    return kind


def _locked_settings(organization: Any, expected_revision: int) -> Any:
    from .models import OrganizationSettings
    from .organization_settings import revision_of

    record = OrganizationSettings.objects.select_for_update().filter(organization=organization).first()
    if revision_of(record) != expected_revision:
        raise ConcurrentUpdate("Organization settings changed.")
    return record


def _check_revision(expected_revision: Any) -> None:
    if type(expected_revision) is not int:
        raise ValidationError({"revision": "Informe a revisão."})


def _delete_after_commit(key: str) -> None:
    from skeleton.files.storage import delete

    if key:
        transaction.on_commit(lambda: delete(key))


def replace_logo(*, actor: Any, upload: IO[bytes], expected_revision: int) -> Any:
    """Store a new logo and point the settings at it (CAS); the old object goes after commit.

    The bytes are written before the row. If the row fails here the object is deleted at
    once; a rollback of the caller's transaction later leaves an orphan that ``files.orphans``
    removes after the grace period.
    """
    from skeleton.access.services import organization_lock
    from skeleton.files.storage import StorageError, delete, put_stream

    from .models import OrganizationSettings

    _check_revision(expected_revision)
    _checked_logo(upload)
    with organization_lock(actor) as organization:
        record = _locked_settings(organization, expected_revision)
        key = logo_key(organization.pk)
        try:
            put_stream(key, upload, max_bytes=LOGO_MAX_BYTES)
        except StorageError:
            raise ValidationError({"logo": "Não foi possível guardar o logo."}) from None
        previous = record.logo_key if record is not None else ""
        try:
            with transaction.atomic():
                if record is None:
                    record = OrganizationSettings(organization=organization, logo_key=key)
                    record.save()
                else:
                    record.logo_key = key
                    record.save(update_fields={"logo_key"})
        except BaseException:
            delete(key)
            raise
        _delete_after_commit(previous)
        return record


def remove_logo(*, actor: Any, expected_revision: int) -> Any:
    """Clear the logo (CAS); the object is deleted after the commit."""
    from skeleton.access.services import organization_lock

    _check_revision(expected_revision)
    with organization_lock(actor) as organization:
        record = _locked_settings(organization, expected_revision)
        if record is None or not record.logo_key:
            return record
        previous = record.logo_key
        record.logo_key = ""
        record.save(update_fields={"logo_key"})
        _delete_after_commit(previous)
        return record


def open_logo() -> tuple[Any, str] | None:
    """The logo of the context organization as (record, MIME type), or None."""
    from skeleton.files.sniffing import HEAD_BYTES, detect

    from .organization_settings import current_settings

    record = current_settings()
    if record is None or not record.logo_key:
        return None
    from skeleton.files.storage import StorageError, open_object

    try:
        with open_object(record.logo_key) as stream:
            head = stream.read(HEAD_BYTES)
    except (StorageError, FileNotFoundError, OSError):
        return None
    for name in ("logo.png", "logo.jpg", "logo.webp"):
        kind = detect(head, name)
        if kind in LOGO_TYPES:
            return record, kind
    return None
