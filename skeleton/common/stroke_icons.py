"""The stroke icon an app ships for its menu entries (#842): pure Python, stdlib only.

The icons of the core are 24×24 outlines drawn with ``currentColor`` (``components/icon.html``).
An app icon has to look like them and must never carry active content, so this accepts only that
shape: an ``<svg>`` with ``viewBox="0 0 24 24"`` and up to ``MAX_SHAPES`` direct children among
``path``, ``circle``, ``ellipse``, ``line``, ``polyline``, ``polygon`` and ``rect``, each with
geometry attributes only (numbers and path data). Fill, colors, style, ids, references, text,
groups, scripts, DOCTYPE and entities are refused.

The result is **rebuilt** from the parsed geometry (``<path d="…"/>…``), never the original
bytes: the core wraps it in its own ``<svg>`` with the stroke of the other icons, so the same
markup is also what two icons are compared by (one concept, one icon). A problem is one of
``CODES`` and names the element or attribute, never a value.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from html import escape

__all__ = ["CODES", "MAX_BYTES", "MAX_SHAPES", "StrokeIconError", "stroke_icon_markup"]

MAX_BYTES = 8 * 1024
MAX_SHAPES = 32
SVG = "http://www.w3.org/2000/svg"
# Geometry of each shape, in the order the markup is rebuilt.
SHAPES: dict[str, tuple[str, ...]] = {
    "path": ("d",),
    "circle": ("cx", "cy", "r"),
    "ellipse": ("cx", "cy", "rx", "ry"),
    "line": ("x1", "y1", "x2", "y2"),
    "polyline": ("points",),
    "polygon": ("points",),
    "rect": ("x", "y", "width", "height", "rx", "ry"),
}
REQUIRED: dict[str, tuple[str, ...]] = {
    "path": ("d",),
    "circle": ("r",),
    "ellipse": ("rx", "ry"),
    "line": (),
    "polyline": ("points",),
    "polygon": ("points",),
    "rect": ("width", "height"),
}
# Presentation the core sets itself on its wrapper: accepted (exports carry them) and dropped.
ROOT_PRESENTATION = {
    "fill": {"none"},
    "stroke": {"currentColor", "currentcolor"},
    "stroke-width": None,
    "stroke-linecap": {"round", "butt", "square"},
    "stroke-linejoin": {"round", "miter", "bevel"},
    "width": None,
    "height": None,
    "class": None,
    "aria-hidden": {"true"},
    "focusable": {"false"},
}
SHAPE_PRESENTATION = {
    "stroke-linecap": {"round", "butt", "square"},
    "stroke-linejoin": {"round", "miter", "bevel"},
}
CODES = (
    "too_large",
    "encoding",
    "doctype_or_pi",
    "malformed",
    "not_svg",
    "view_box",
    "attribute",
    "element",
    "value",
    "text",
    "empty",
    "too_complex",
)
_NUMBERS = re.compile(r"[0-9.,eE+\-\s]+")
_PATH_DATA = re.compile(r"[0-9.,eE+\-\sMmLlHhVvCcSsQqTtAaZz]+")
_SIZE = re.compile(r"[0-9.]+(?:px|em|rem|%)?")
_DECLARATION = re.compile(r"<\?xml\s[^?]*\?>")


class StrokeIconError(ValueError):
    """What the icon breaks: one of ``CODES`` and the local name of the element or attribute."""

    def __init__(self, code: str, item: str = "") -> None:
        super().__init__(f"{code}: {item}" if item else code)
        self.code = code
        self.item = item


def _local(name: str) -> tuple[str, str]:
    if name.startswith("{"):
        namespace, _, local = name[1:].partition("}")
        return namespace, local
    return "", name


def _text(content: bytes) -> str:
    if len(content) > MAX_BYTES:
        raise StrokeIconError("too_large")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise StrokeIconError("encoding") from None
    text = text.removeprefix("﻿").lstrip()
    declaration = _DECLARATION.match(text)
    body = text[declaration.end() :] if declaration else text
    # No DOCTYPE, ENTITY, CDATA nor processing instruction; comments are allowed (and dropped).
    if "<?" in body or re.search(r"<!(?!--)", body):
        raise StrokeIconError("doctype_or_pi")
    return text


def _view_box(root: ElementTree.Element) -> None:
    value = root.attrib.get("viewBox")
    if value is None or [part for part in re.split(r"[\s,]+", value.strip()) if part] != ["0", "0", "24", "24"]:
        raise StrokeIconError("view_box", "viewBox")


def _root_attributes(root: ElementTree.Element) -> None:
    for name, value in root.attrib.items():
        namespace, local = _local(name)
        if namespace or local == "viewBox":
            if namespace:
                raise StrokeIconError("attribute", local)
            continue
        if local not in ROOT_PRESENTATION:
            raise StrokeIconError("attribute", local)
        allowed = ROOT_PRESENTATION[local]
        if local in {"width", "height", "stroke-width"}:
            if not _SIZE.fullmatch(value.strip()):
                raise StrokeIconError("value", local)
        elif allowed is not None and value.strip() not in allowed:
            raise StrokeIconError("value", local)


def _shape(element: ElementTree.Element) -> str:
    namespace, tag = _local(element.tag)
    if namespace != SVG or tag not in SHAPES:
        raise StrokeIconError("element", tag)
    if len(element) or (element.text or "").strip() or (element.tail or "").strip():
        raise StrokeIconError("text", tag)
    values: dict[str, str] = {}
    for name, value in element.attrib.items():
        attribute_namespace, local = _local(name)
        if attribute_namespace:
            raise StrokeIconError("attribute", local)
        if local in SHAPE_PRESENTATION:
            if value.strip() not in SHAPE_PRESENTATION[local]:
                raise StrokeIconError("value", local)
            continue
        if local not in SHAPES[tag]:
            raise StrokeIconError("attribute", local)
        value = " ".join(value.split())
        pattern = _PATH_DATA if local == "d" else _NUMBERS
        if not value or not pattern.fullmatch(value):
            raise StrokeIconError("value", local)
        values[local] = value
    if any(name not in values for name in REQUIRED[tag]):
        raise StrokeIconError("attribute", tag)
    attributes = "".join(f' {name}="{escape(values[name])}"' for name in SHAPES[tag] if name in values)
    return f"<{tag}{attributes}/>"


def stroke_icon_markup(content: bytes) -> str:
    """The rebuilt inner markup of a valid stroke icon; raises ``StrokeIconError`` otherwise."""
    text = _text(content)
    try:
        # _text refused DOCTYPE, ENTITY and processing instructions, so no entity can be declared
        # or expanded; the size is bounded. That is what defusedxml would add here.
        root = ElementTree.fromstring(text)  # noqa: S314
    except ElementTree.ParseError:
        raise StrokeIconError("malformed") from None
    namespace, tag = _local(root.tag)
    if namespace != SVG or tag != "svg":
        raise StrokeIconError("not_svg")
    if (root.text or "").strip():
        raise StrokeIconError("text", "svg")
    _view_box(root)
    _root_attributes(root)
    shapes = list(root)
    if not shapes:
        raise StrokeIconError("empty")
    if len(shapes) > MAX_SHAPES:
        raise StrokeIconError("too_complex")
    return "".join(_shape(element) for element in shapes)
