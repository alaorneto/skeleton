"""Installing, updating and removing theme packages (#635): only the platform administrator.

``install_theme_package`` reads the zip with ``package_format`` (nothing is written before the
whole package is valid), checks the AA contrast and the core range, stores every image and font
in private storage under its own ``platform/branding/<uuid4>`` key and writes one
``InstanceTheme`` with ``source="package"``. Sending the same content again writes nothing; a
higher version updates the same row (same ``sysid``; an active theme stays active); the same
version with other content and a lower version are refused.

Bytes are written before the row; if the row fails, the new objects are deleted at once. The
objects of a replaced or removed package are never deleted inside the transaction: they become
orphans and ``appearance.orphans`` deletes them after the grace period, so a rolled back caller
never leaves a row pointing at a deleted file. Removing is ``services.delete_theme`` (soft delete,
the active theme is refused), the same as for a form theme.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from skeleton.common.exceptions import ConcurrentUpdate

from . import services
from .package_format import MAX_ARCHIVE_BYTES, ThemePackage, ThemePackageError
from .package_messages import describe, package_error_message
from .services import _require_platform
from .snapshot import reset_appearance_cache

__all__ = ["InstallResult", "install_theme_package", "refused", "stored_files"]

PACKAGE_FIELDS = (
    "name",
    "brand_color",
    "radius",
    "font",
    "custom_css",
    "source",
    "package_id",
    "package_version",
    "package_digest",
    "package_manifest",
    "package_files",
    "font_family",
    "product_name",
    "footer_text",
)


@dataclass(frozen=True, slots=True)
class InstallResult:
    theme: Any
    outcome: str  # "installed" | "updated" | "unchanged"
    previous_version: str = ""


def refused(code: str, *, path: str = "", line: int = 0, item: str = "") -> ValidationError:
    """The ``ValidationError`` of the ``package`` field, with the code of the package error."""
    message = package_error_message(code, path=path, line=line, item=item)
    return ValidationError({"package": [ValidationError(message, code=code)]})


def _read(data: Any) -> ThemePackage:
    from skeleton.tenancy.branding import validate_brand_color
    from skeleton.versioning import core_version

    from .package_format import check_core_compatibility, read_theme_archive

    if not isinstance(data, (bytes, bytearray)):
        raise refused("archive")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise refused("too_large")
    try:
        package = read_theme_archive(bytes(data))
        check_core_compatibility(package, core_version()["version"])
    except ThemePackageError as error:
        message = describe(error)
        raise ValidationError({"package": [ValidationError(message, code=error.code)]}) from None
    if package.brand_color:
        try:
            validate_brand_color(package.brand_color)
        except ValidationError:
            raise refused("brand_color") from None
    return package


def _store(package: ThemePackage) -> tuple[dict[str, Any], list[str]]:
    """Write every image and font; ``(package_files, new keys)``. On a failure, nothing is left."""
    from skeleton.files.storage import StorageError, delete, put_stream

    from .images import image_key

    written: list[str] = []

    def put(content: bytes) -> str:
        key = image_key()
        put_stream(key, io.BytesIO(content), max_bytes=len(content))
        written.append(key)
        return key

    files: dict[str, Any] = {}
    try:
        for image in package.images:
            files[image.kind] = {
                "key": put(image.content),
                "file": image.file,
                "mime": image.mime,
                "sha256": image.sha256,
            }
        if package.fonts:
            files["fonts"] = [
                {
                    "family": font.family,
                    "file": font.file,
                    "weight": font.weight,
                    "style": font.style,
                    "key": put(font.content),
                    "sha256": font.sha256,
                }
                for font in package.fonts
            ]
    except (StorageError, OSError):
        for key in written:
            delete(key)
        raise refused("archive") from None
    return files, written


def stored_files(theme: Any) -> list[str]:
    """Every storage key of a package theme (images and fonts)."""
    files = getattr(theme, "package_files", None) or {}
    keys = [entry.get("key", "") for kind, entry in files.items() if kind != "fonts" and isinstance(entry, dict)]
    keys += [entry.get("key", "") for entry in files.get("fonts", []) if isinstance(entry, dict)]
    return [key for key in keys if key]


def _values(package: ThemePackage, files: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": package.name,
        "brand_color": package.brand_color,
        "radius": package.radius,
        "font": package.font,
        "custom_css": package.custom_css,
        "source": "package",
        "package_id": package.id,
        "package_version": package.version,
        "package_digest": package.content_digest,
        "package_manifest": package.manifest,
        "package_files": files,
        "font_family": package.font_family,
        "product_name": package.product_name,
        "footer_text": package.footer_text,
    }


def _version(value: str) -> Any:
    from packaging.version import Version

    from skeleton.versioning import normalize_version

    return Version(normalize_version(value))


def _save(record: Any, written: list[str], **kwargs: Any) -> None:
    from skeleton.files.storage import delete

    try:
        with transaction.atomic():
            record.save(**kwargs)
    except IntegrityError as error:
        for key in written:
            delete(key)
        if "sk_appearance_theme_package_live" in str(error):
            raise ConcurrentUpdate("Theme package installed concurrently.") from None
        raise refused("name_taken") from None
    except BaseException:
        for key in written:
            delete(key)
        raise


def install_theme_package(*, actor: Any, data: bytes) -> InstallResult:
    """Install, update or recognize a theme package (see the module docstring); never activates it."""
    from .models import InstanceBrand, InstanceTheme

    _require_platform(actor)
    package = _read(data)
    with transaction.atomic():
        # The brand row serializes installs with activations (it may not exist yet).
        InstanceBrand.objects.select_for_update().filter(slot="instance").first()
        existing = InstanceTheme.objects.select_for_update().filter(source="package", package_id=package.id).first()
        others = InstanceTheme.objects.filter(name=package.name)
        if existing is not None:
            if existing.package_digest == package.content_digest:
                return InstallResult(existing, "unchanged", existing.package_version)
            new, old = _version(package.version), _version(existing.package_version)
            if new == old:
                raise refused("same_version_changed")
            if new < old:
                raise refused("downgrade")
            if others.exclude(pk=existing.pk).exists():
                raise refused("name_taken")
            previous = existing.package_version
            files, written = _store(package)
            values = _values(package, files)
            changed = {name for name in PACKAGE_FIELDS if getattr(existing, name) != values[name]}
            for name in changed:
                setattr(existing, name, values[name])
            _save(existing, written, update_fields=changed)
            transaction.on_commit(reset_appearance_cache)
            return InstallResult(existing, "updated", previous)
        if InstanceTheme.objects.count() >= services.MAX_THEMES:
            raise refused("too_many_themes")
        if others.exists():
            raise refused("name_taken")
        files, written = _store(package)
        record = InstanceTheme(**_values(package, files))
        _save(record, written)
        transaction.on_commit(reset_appearance_cache)
    return InstallResult(record, "installed")
