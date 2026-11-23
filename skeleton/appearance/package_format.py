"""The theme package, format 1 (#633): a zip (or a directory) with ``theme.json``, CSS, images and fonts.

``read_theme_archive(data)`` and ``read_theme_directory(path)`` return an immutable
``ThemePackage`` with everything validated, or raise ``ThemePackageError(code, path, line, item)``.
Nothing is written here and no file name becomes a disk path: the bytes stay in memory.

This module never imports Django: it also runs inside ``package build``/``package doctor`` of apps
(epic #630), which work without settings. So the AA contrast of the colour (it needs
``skeleton.tenancy.branding``) is checked by whoever installs the package, not here; this checks
only the ``#rrggbb`` format. The layout and the field rules are documented in
``docs/appearance.md`` ("Pacote de tema"); ``ERROR_CODES`` lists every code a surface may need to
translate (``package_messages``).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from skeleton.appearance.css_validation import css_problem
from skeleton.appearance.svg_validation import MAX_BYTES as SVG_MAX_BYTES
from skeleton.appearance.svg_validation import svg_problem
from skeleton.files.sniffing import HEAD_BYTES, detect
from skeleton.system.packaging.errors import PackageError
from skeleton.system.packaging.limits import STABLE_ID
from skeleton.system.packaging.zipio import _read_member, _zip_members
from skeleton.versioning import normalize_version, validate_specifier, version_satisfies

__all__ = [
    "ERROR_CODES",
    "FORMAT",
    "MAX_ARCHIVE_BYTES",
    "PackageFont",
    "PackageImage",
    "ThemePackage",
    "ThemePackageError",
    "archive_files",
    "check_core_compatibility",
    "directory_files",
    "from_files",
    "read_theme_archive",
    "read_theme_directory",
]

FORMAT = 1
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_UNPACKED_BYTES = 6 * 1024 * 1024
MAX_MEMBERS = 40
MAX_EXPANSION_RATIO = 100
MAX_MANIFEST_BYTES = 16 * 1024
LOGO_MAX_BYTES = 512 * 1024  # The same limit as skeleton.tenancy.branding.LOGO_MAX_BYTES.
ICON_MAX_BYTES = 256 * 1024  # The same limit as skeleton.tenancy.branding.ICON_MAX_BYTES.
ICON_SIDE = 512
FONT_MAX_BYTES = 512 * 1024
MAX_FONTS = 8
MANIFEST = "theme.json"
DEFAULT_CSS = "theme.css"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
RASTER_LOGO = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
RADII = ("sharp", "default", "round")
FONTS = ("default", "system", "serif")
# The name of the core font before #690: packages made for older cores keep it, read as "default".
FONT_ALIASES = {"inter": "default"}


def canonical_font(value: Any) -> Any:
    """``default`` for the legacy alias of the core font; anything else unchanged (validated by the caller)."""
    return FONT_ALIASES.get(value, value) if isinstance(value, str) else value


STYLES = ("normal", "italic")
GENERIC_FAMILIES = frozenset(
    {
        "serif",
        "sans-serif",
        "monospace",
        "cursive",
        "fantasy",
        "system-ui",
        "ui-sans-serif",
        "ui-serif",
        "ui-monospace",
        "ui-rounded",
        "math",
        "emoji",
        "fangsong",
        "inherit",
        "initial",
        "unset",
        "revert",
        "revert-layer",
        "default",
    }
)
ERROR_CODES = (
    "archive",
    "too_large",
    "too_many_files",
    "unexpected_file",
    "missing_manifest",
    "manifest_json",
    "manifest_field",
    "unknown_field",
    "format",
    "id",
    "version",
    "name",
    "text_field",
    "publisher",
    "compatibility",
    "brand_color",
    "radius",
    "font",
    "font_family",
    "image_path",
    "image_type",
    "image_size",
    "svg",
    "icon",
    "logo_dark_without_logo",
    "too_many_fonts",
    "font_family_name",
    "font_file",
    "font_weight",
    "font_style",
    "css_missing",
    "css",
    "incompatible_core",
)
FIELDS: tuple[str, ...] = ("format", "id", "version", "name", "description", "publisher", "compatibility")
FIELDS += ("tokens", "brand", "images", "fonts", "css")
REQUIRED = ("format", "id", "version", "name", "compatibility")
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]*")
_LICENSE = re.compile(r"LICENSES/[A-Za-z0-9][A-Za-z0-9._-]*\.txt")
_FAMILY = re.compile(r"[A-Za-z][A-Za-z0-9 -]{0,62}[A-Za-z0-9]")
_WEIGHT = re.compile(r"[1-9]00")
_COLOR = re.compile(r"#[0-9a-f]{6}")
_UNUSED = frozenset({"README.md"})


class ThemePackageError(ValueError):
    """A theme package that cannot be installed: ``code`` is one of ``ERROR_CODES``.

    ``path`` is the file inside the package, ``line`` a 1-based line (CSS), ``item`` the field,
    the forbidden CSS item or the SVG problem code. ``detail`` is technical text for the author
    console only; surfaces never show it.
    """

    def __init__(self, code: str, *, path: str = "", line: int = 0, item: str = "", detail: str = ""):
        super().__init__(f"{code} {path}:{line} {item}".strip())
        self.code = code
        self.path = path
        self.line = line
        self.item = item
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PackageImage:
    kind: str  # "logo" | "logo_dark" | "icon"
    file: str
    mime: str
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class PackageFont:
    family: str
    file: str
    weight: str
    style: str
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class ThemePackage:
    id: str
    version: str
    name: str
    description: str
    publisher: str
    compatibility: str
    brand_color: str
    radius: str
    font: str
    font_family: str
    product_name: str
    footer_text: str
    custom_css: str
    images: tuple[PackageImage, ...]
    fonts: tuple[PackageFont, ...]
    manifest: dict[str, Any] = field(hash=False, compare=False)
    content_digest: str = ""

    def image(self, kind: str) -> PackageImage | None:
        return next((image for image in self.images if image.kind == kind), None)


def _fail(code: str, **kwargs: Any) -> NoReturn:
    raise ThemePackageError(code, **kwargs)


# --------------------------------------------------------------------------- reading the files


def _allowed_path(path: str) -> bool:
    if path in _UNUSED or _LICENSE.fullmatch(path):
        return True
    return all(_SEGMENT.fullmatch(part) for part in path.split("/"))


def read_theme_archive(data: bytes) -> ThemePackage:
    """The package in the bytes of a zip, validated; ``ThemePackageError`` otherwise."""
    return from_files(archive_files(data))


def archive_files(data: bytes) -> dict[str, bytes]:
    """Every file of a zip (path → bytes) within the archive limits; nothing about the theme yet."""
    if not isinstance(data, (bytes, bytearray)):
        _fail("archive")
    if len(data) > MAX_ARCHIVE_BYTES:
        _fail("too_large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
    except (zipfile.BadZipFile, OSError, ValueError) as error:
        raise ThemePackageError("archive", detail=str(error)) from error
    with archive:
        if len(archive.infolist()) > MAX_MEMBERS:
            _fail("too_many_files")
        total = 0
        for info in archive.infolist():
            if not info.is_dir() and info.file_size > max(1, info.compress_size) * MAX_EXPANSION_RATIO:
                _fail("archive", path=info.filename, detail="Expansion ratio.")
            total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            _fail("too_large")
        try:
            members = _zip_members(archive, maximum=MAX_MEMBERS)
            files = {name: _read_member(archive, info, maximum=MAX_UNPACKED_BYTES) for name, info in members.items()}
        except PackageError as error:
            raise ThemePackageError("archive", detail=str(error)) from error
    return files


def read_theme_directory(path: str | os.PathLike[str]) -> ThemePackage:
    """The package in a directory (an app theme, epic #630, or a source tree), with the same rules."""
    return from_files(directory_files(path))


def directory_files(path: str | os.PathLike[str]) -> dict[str, bytes]:
    """Every file of a directory (path → bytes) with the archive rules: no links, no special files."""
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        _fail("missing_manifest")
    files: dict[str, bytes] = {}
    total = 0
    for current, directories, names in os.walk(root, followlinks=False):
        for name in sorted(directories):
            candidate = Path(current) / name
            if candidate.is_symlink():
                _fail("archive", path=candidate.relative_to(root).as_posix(), detail="Symbolic link.")
        for name in sorted(names):
            source = Path(current) / name
            relative = source.relative_to(root).as_posix()
            mode = os.lstat(source).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                _fail("archive", path=relative, detail="Link or special file.")
            if len(files) >= MAX_MEMBERS:
                _fail("too_many_files")
            size = os.lstat(source).st_size
            total += size
            if total > MAX_UNPACKED_BYTES:
                _fail("too_large")
            with open(source, "rb") as stream:
                files[relative] = stream.read(MAX_UNPACKED_BYTES + 1)
    return files


# --------------------------------------------------------------------------- the manifest


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key.")
        result[key] = value
    return result


def _manifest(files: dict[str, bytes]) -> dict[str, Any]:
    raw = files.get(MANIFEST)
    if raw is None:
        _fail("missing_manifest")
    if len(raw) > MAX_MANIFEST_BYTES:
        _fail("manifest_json", path=MANIFEST)
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, ValueError) as error:
        raise ThemePackageError("manifest_json", path=MANIFEST, detail=str(error)) from error
    if not isinstance(value, dict):
        _fail("manifest_json", path=MANIFEST)
    return value


def _object(value: Any, allowed: tuple[str, ...], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("manifest_field", path=MANIFEST, item=name)
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _fail("unknown_field", path=MANIFEST, item=f"{name}.{unknown[0]}" if name else unknown[0])
    return value


def _line_text(value: Any, limit: int, *, code: str, item: str, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or any(mark in value for mark in ("\n", "\r", "\x00")):
        _fail(code, path=MANIFEST, item=item)
    text = value.strip()
    if len(text) > limit or (required and not text):
        _fail(code, path=MANIFEST, item=item)
    return text


def _choice(value: Any, choices: tuple[str, ...], default: str, code: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in choices:
        _fail(code, path=MANIFEST)
    return value


def _header(manifest: dict[str, Any]) -> dict[str, Any]:
    _object(manifest, FIELDS, "")
    missing = [name for name in REQUIRED if name not in manifest]
    if missing:
        _fail("manifest_field", path=MANIFEST, item=missing[0])
    if type(manifest["format"]) is not int or manifest["format"] != FORMAT:
        _fail("format", path=MANIFEST)
    identity = manifest["id"]
    if not isinstance(identity, str) or not STABLE_ID.fullmatch(identity):
        _fail("id", path=MANIFEST)
    version = manifest["version"]
    try:
        normalize_version(version)
    except (TypeError, ValueError) as error:
        raise ThemePackageError("version", path=MANIFEST) from error
    publisher = manifest.get("publisher")
    if publisher is not None and (not isinstance(publisher, str) or not STABLE_ID.fullmatch(publisher)):
        _fail("publisher", path=MANIFEST)
    compatibility = _object(manifest["compatibility"], ("skeleton",), "compatibility")
    try:
        specifier = validate_specifier(compatibility.get("skeleton"))
    except (TypeError, ValueError) as error:
        raise ThemePackageError("compatibility", path=MANIFEST) from error
    return {
        "format": FORMAT,
        "id": identity,
        "version": version,
        "name": _line_text(manifest["name"], 80, code="name", item="name", required=True),
        "description": _line_text(manifest.get("description"), 300, code="text_field", item="description"),
        "publisher": publisher or "",
        "compatibility": {"skeleton": specifier},
    }


def _tokens(manifest: dict[str, Any]) -> dict[str, str]:
    tokens = _object(manifest.get("tokens", {}), ("brand_color", "radius", "font", "font_family"), "tokens")
    color = tokens.get("brand_color", "")
    if not isinstance(color, str) or (color and not _COLOR.fullmatch(color.lower())):
        _fail("brand_color", path=MANIFEST)
    family = tokens.get("font_family", "")
    if not isinstance(family, str):
        _fail("font_family", path=MANIFEST)
    return {
        "brand_color": color.lower(),
        "radius": _choice(tokens.get("radius"), RADII, "default", "radius"),
        "font": _choice(canonical_font(tokens.get("font")), FONTS, "default", "font"),
        "font_family": family,
    }


def _brand(manifest: dict[str, Any]) -> dict[str, str]:
    brand = _object(manifest.get("brand", {}), ("product_name", "footer_text"), "brand")
    return {
        "product_name": _line_text(brand.get("product_name"), 60, code="text_field", item="brand.product_name"),
        "footer_text": _line_text(brand.get("footer_text"), 160, code="text_field", item="brand.footer_text"),
    }


# --------------------------------------------------------------------------- files the manifest names


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _declared(files: dict[str, bytes], path: Any, folder: str, code: str) -> tuple[str, bytes]:
    if not isinstance(path, str) or not path.startswith(folder + "/") or not _allowed_path(path):
        _fail(code, path=str(path)[:200])
    if path not in files:
        _fail(code, path=path)
    return path, files[path]


def _logo(files: dict[str, bytes], kind: str, path: Any) -> PackageImage:
    path, content = _declared(files, path, "images", "image_path")
    suffix = PurePosixPath(path).suffix
    if suffix == ".svg":
        if len(content) > SVG_MAX_BYTES:
            _fail("image_size", path=path)
        problem = svg_problem(content)
        if problem is not None:
            _fail("svg", path=path, item=problem.code)
        return PackageImage(kind, path, "image/svg+xml", content, _sha(content))
    if suffix not in RASTER_LOGO:
        _fail("image_path", path=path)
    if len(content) > LOGO_MAX_BYTES:
        _fail("image_size", path=path)
    mime = detect(content[:HEAD_BYTES], path)
    if mime is None or mime not in set(RASTER_LOGO.values()):
        _fail("image_type", path=path)
    return PackageImage(kind, path, mime, content, _sha(content))


def _icon(files: dict[str, bytes], path: Any) -> PackageImage:
    path, content = _declared(files, path, "images", "image_path")
    if PurePosixPath(path).suffix != ".png":
        _fail("image_path", path=path)
    if len(content) > ICON_MAX_BYTES:
        _fail("image_size", path=path)
    if (
        not content.startswith(PNG_SIGNATURE)
        or len(content) < 24
        or content[12:16] != b"IHDR"
        or struct.unpack(">II", content[16:24]) != (ICON_SIDE, ICON_SIDE)
    ):
        _fail("icon", path=path)
    return PackageImage("icon", path, "image/png", content, _sha(content))


def _images(files: dict[str, bytes], manifest: dict[str, Any]) -> tuple[PackageImage, ...]:
    images = _object(manifest.get("images", {}), ("logo", "logo_dark", "icon"), "images")
    if "logo_dark" in images and "logo" not in images:
        _fail("logo_dark_without_logo", path=MANIFEST)
    found = []
    for kind in ("logo", "logo_dark"):
        if kind in images:
            found.append(_logo(files, kind, images[kind]))
    if "icon" in images:
        found.append(_icon(files, images["icon"]))
    return tuple(found)


def _weight(value: Any) -> str:
    if value is None:
        return "400"
    if isinstance(value, str):
        parts = value.split(" ")
        if len(parts) == 1 and _WEIGHT.fullmatch(parts[0]):
            return value
        if len(parts) == 2 and all(_WEIGHT.fullmatch(part) for part in parts) and int(parts[0]) < int(parts[1]):
            return value
    _fail("font_weight", path=MANIFEST)
    return ""


def _fonts(files: dict[str, bytes], manifest: dict[str, Any]) -> tuple[PackageFont, ...]:
    entries = manifest.get("fonts", [])
    if not isinstance(entries, list):
        _fail("manifest_field", path=MANIFEST, item="fonts")
    if len(entries) > MAX_FONTS:
        _fail("too_many_fonts", path=MANIFEST)
    fonts = []
    seen: set[str] = set()
    for entry in entries:
        entry = _object(entry, ("family", "file", "weight", "style"), "fonts[]")
        family = entry.get("family")
        if (
            not isinstance(family, str)
            or not _FAMILY.fullmatch(family)
            or family.casefold() in GENERIC_FAMILIES
            or "  " in family
        ):
            _fail("font_family_name", path=MANIFEST)
        path, content = _declared(files, entry.get("file"), "fonts", "font_file")
        if PurePosixPath(path).suffix != ".woff2" or path.count("/") != 1 or path in seen:  # Flat: the name is the URL.
            _fail("font_file", path=path)
        if len(content) > FONT_MAX_BYTES or not content.startswith(b"wOF2"):
            _fail("font_file", path=path)
        seen.add(path)
        weight = _weight(entry.get("weight"))
        style = entry.get("style", "normal")
        if style not in STYLES:
            _fail("font_style", path=MANIFEST)
        fonts.append(PackageFont(family, path, weight, style, content, _sha(content)))
    return tuple(fonts)


def _css(files: dict[str, bytes], manifest: dict[str, Any]) -> tuple[str, str]:
    """``(file, text)`` of the custom CSS; ``("", "")`` when there is none."""
    declared = "css" in manifest
    name = manifest.get("css", DEFAULT_CSS)
    if not isinstance(name, str) or "/" in name or not name.endswith(".css") or not _SEGMENT.fullmatch(name):
        _fail("css_missing", path=str(name)[:200])
    if name not in files:
        if declared:
            _fail("css_missing", path=name)
        return "", ""
    try:
        text = files[name].decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ThemePackageError("css", path=name, item="not_text") from error
    problem = css_problem(text)
    if problem is not None:
        _fail("css", path=name, line=problem.line, item=problem.item or problem.code)
    return name, text.replace("\r\n", "\n")


def _digest(manifest: dict[str, Any], used: dict[str, bytes]) -> str:
    hasher = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    for path in sorted(used):
        hasher.update(f"{path}\0{_sha(used[path])}\n".encode())
    return hasher.hexdigest()


def from_files(files: dict[str, bytes]) -> ThemePackage:
    """The package made of ``files`` (path → bytes), validated (the exporter builds them in memory)."""
    manifest = _manifest(files)  # First: a zip of the folder (aurora/theme.json) says "no manifest".
    for path in files:
        if path != MANIFEST and not _allowed_path(path):
            _fail("unexpected_file", path=path[:200])
    header = _header(manifest)
    tokens = _tokens(manifest)
    brand = _brand(manifest)
    images = _images(files, manifest)
    fonts = _fonts(files, manifest)
    families = {font.family for font in fonts}
    if tokens["font_family"] and tokens["font_family"] not in families:
        _fail("font_family", path=MANIFEST)
    css_file, css_text = _css(files, manifest)
    used = {image.file: image.content for image in images} | {font.file: font.content for font in fonts}
    if css_file:
        used[css_file] = files[css_file]
    for path in files:
        if path != MANIFEST and path not in used and path not in _UNUSED and not _LICENSE.fullmatch(path):
            _fail("unexpected_file", path=path)
    canonical: dict[str, Any] = {
        **header,
        "tokens": tokens,
        "brand": brand,
        "images": {image.kind: image.file for image in images},
        "fonts": [{"family": f.family, "file": f.file, "weight": f.weight, "style": f.style} for f in fonts],
    }
    if css_file:
        canonical["css"] = css_file
    return ThemePackage(
        id=header["id"],
        version=header["version"],
        name=header["name"],
        description=header["description"],
        publisher=header["publisher"],
        compatibility=header["compatibility"]["skeleton"],
        brand_color=tokens["brand_color"],
        radius=tokens["radius"],
        font=tokens["font"],
        font_family=tokens["font_family"],
        product_name=brand["product_name"],
        footer_text=brand["footer_text"],
        custom_css=css_text,
        images=images,
        fonts=fonts,
        manifest=canonical,
        content_digest=_digest(canonical, used),
    )


def check_core_compatibility(package: ThemePackage, version: str | None) -> None:
    """``incompatible_core`` when ``version`` (the installed core) is outside the package range.

    Same rule as an app's ``compatibility.skeleton``: an unknown core version is incompatible.
    """
    if not version or not version_satisfies(version, package.compatibility):
        _fail("incompatible_core", path=MANIFEST)
