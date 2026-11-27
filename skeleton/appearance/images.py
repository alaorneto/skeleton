"""Logo, dark-mode logo and icon of the instance (#596), in the private storage.

Same limits and checks as the organization images (``skeleton.tenancy.branding``): the logos are
PNG, JPEG or WebP up to 512 KB, the icon a 512 × 512 PNG up to 256 KB, always proven by the
content. Keys are ``platform/branding/<uuid4>``, generated here and never chosen by anyone. The
bytes are written before the row; a failure writing the row deletes the object at once, the
replaced object goes after the commit, and a rollback of the caller leaves an orphan that the
global job ``appearance.orphans`` removes after the grace period.
"""

from __future__ import annotations

import re
import uuid
from typing import IO, Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext

from .services import _create_brand, _locked_brand, _require_platform
from .snapshot import reset_appearance_cache

KINDS = {"logo": "logo_key", "logo_dark": "logo_dark_key", "icon": "icon_key"}
PREFIX = "platform/branding/"
_KEY = re.compile(r"^platform/branding/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LOGO_NAMES = ("logo.png", "logo.jpg", "logo.webp")


def image_key() -> str:
    return f"{PREFIX}{uuid.uuid4()}"


def is_image_key(key: Any) -> bool:
    return isinstance(key, str) and _KEY.fullmatch(key) is not None


def _kind(kind: Any) -> str:
    if kind not in KINDS:
        raise ValidationError({"image": gettext("Imagem desconhecida.")})
    return KINDS[kind]


def _checked(kind: str, upload: Any) -> int:
    """The size limit of ``kind`` after checking the content; errors carry the key of the kind."""
    from skeleton.tenancy.branding import ICON_MAX_BYTES, LOGO_MAX_BYTES, checked_icon, checked_logo

    try:
        if kind == "icon":
            checked_icon(upload)
            return ICON_MAX_BYTES
        checked_logo(upload)
        return LOGO_MAX_BYTES
    except ValidationError as error:
        flat = (
            [message for messages in error.message_dict.values() for message in messages]
            if hasattr(error, "error_dict")
            else list(error.messages)
        )
        raise ValidationError({kind: flat}) from None


def _delete_after_commit(key: str) -> None:
    from skeleton.files.storage import delete

    if key:
        transaction.on_commit(lambda: delete(key))


def replace_image(*, actor: Any, kind: str, upload: IO[bytes], expected_revision: int) -> Any:
    """Store a new image and point the brand row at it (CAS); the old object goes after commit."""
    from skeleton.files.storage import StorageError, delete, put_stream

    _require_platform(actor)
    field = _kind(kind)
    limit = _checked(kind, upload)
    with transaction.atomic():
        record = _locked_brand(expected_revision)
        key = image_key()
        try:
            put_stream(key, upload, max_bytes=limit)
        except StorageError:
            raise ValidationError({kind: gettext("Não foi possível guardar a imagem.")}) from None
        previous = getattr(record, field) if record is not None else ""
        try:
            with transaction.atomic():
                if record is None:
                    record = _create_brand(**{field: key})
                else:
                    setattr(record, field, key)
                    record.save(update_fields={field})
        except BaseException:
            delete(key)
            raise
        _delete_after_commit(previous)
        transaction.on_commit(reset_appearance_cache)
    return record


def remove_image(*, actor: Any, kind: str, expected_revision: int) -> Any:
    """Clear one image (CAS); the object is deleted after the commit."""
    _require_platform(actor)
    field = _kind(kind)
    with transaction.atomic():
        record = _locked_brand(expected_revision)
        if record is None or not getattr(record, field):
            return record
        previous = getattr(record, field)
        setattr(record, field, "")
        record.save(update_fields={field})
        _delete_after_commit(previous)
        transaction.on_commit(reset_appearance_cache)
    return record


def open_image(kind: str) -> tuple[bytes, str] | None:
    """Bytes and MIME type of one image of the brand row, or ``None``; the type is proven again."""
    from skeleton.files.sniffing import HEAD_BYTES, detect
    from skeleton.files.storage import StorageError, open_object
    from skeleton.tenancy.branding import ICON_MAX_BYTES, LOGO_MAX_BYTES, LOGO_TYPES

    from .services import current_brand

    field = KINDS.get(kind)
    record = current_brand() if field else None
    key = getattr(record, field, "") if record is not None and field else ""
    if not is_image_key(key):
        return None
    names: tuple[str, ...]
    if kind == "icon":
        limit, types, names = ICON_MAX_BYTES, frozenset({"image/png"}), ("icon.png",)
    else:
        limit, types, names = LOGO_MAX_BYTES, LOGO_TYPES, LOGO_NAMES
    try:
        with open_object(key) as stream:
            content = stream.read(limit + 1)
    except (StorageError, FileNotFoundError, OSError):
        return None
    if len(content) > limit:
        return None
    for name in names:
        found = detect(content[:HEAD_BYTES], name)
        if found in types:
            return content, found
    return None
