"""The barrier of an SVG logo inside a theme package (#632): pure Python, stdlib only.

SVG is active XML: scripts, ``on*`` handlers, ``<foreignObject>`` with HTML, external references,
entities (expansion bombs) and animations that rewrite ``href``. This accepts only a static subset
(shapes, paths, gradients, text, groups, internal ``use``) and refuses the rest. It never rewrites
the file: the bytes stored are the original ones.

Defence in depth: the logo is only ever shown in ``<img>``/``<picture>`` (browsers do not run
scripts of images) and is served with ``Content-Security-Policy: default-src 'none';
style-src 'unsafe-inline'; sandbox`` and ``nosniff``, so even opened in a tab it runs nothing.
A problem names the element or the attribute (``item``), never a value: values can be large or
hold the attack itself.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import Any

__all__ = ["MAX_BYTES", "SvgProblem", "svg_problem"]

MAX_BYTES = 256 * 1024
MAX_DEPTH = 32
MAX_ELEMENTS = 5000
SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
XML = "http://www.w3.org/XML/1998/namespace"
ELEMENTS = frozenset(
    {
        "svg",
        "g",
        "defs",
        "title",
        "desc",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "linearGradient",
        "radialGradient",
        "stop",
        "clipPath",
        "mask",
        "pattern",
        "symbol",
        "use",
    }
)
TEXT_ELEMENTS = frozenset({"title", "desc", "text", "tspan"})
HREF_ELEMENTS = frozenset({"use", "linearGradient", "radialGradient", "pattern"})
CODES = (
    "too_large",
    "encoding",
    "doctype_or_pi",
    "malformed",
    "too_complex",
    "not_svg",
    "element",
    "attribute",
    "reference",
    "style",
    "text",
)
_LOCAL_REFERENCE = re.compile(r"#[A-Za-z_][\w.-]*")
_LOCAL_URL = re.compile(r"url\(\s*#[A-Za-z_][\w.-]*\s*\)")
_DECLARATION = re.compile(r"<\?xml\s[^?]*\?>")


@dataclass(frozen=True, slots=True)
class SvgProblem:
    """What the SVG breaks: one of ``CODES`` and the local name of the element or attribute."""

    code: str
    item: str = ""


def _local(name: str) -> tuple[str, str]:
    if name.startswith("{"):
        namespace, _, local = name[1:].partition("}")
        return namespace, local
    return "", name


def _decode(content: bytes) -> str | None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text[1:] if text.startswith("﻿") else text


def _prolog_problem(text: str) -> SvgProblem | None:
    """No DOCTYPE, ENTITY, CDATA or processing instruction other than the leading declaration."""
    body = text.lstrip()
    declaration = _DECLARATION.match(body)
    if declaration:
        body = body[declaration.end() :]
    if "<?" in body:
        return SvgProblem("doctype_or_pi")
    index = body.find("<!")
    while index >= 0:
        if not body.startswith("<!--", index):
            return SvgProblem("doctype_or_pi")
        index = body.find("<!", index + 4)
    return None


def _value_problem(value: str) -> bool:
    """A value that loads or runs something: ``javascript:``, ``data:``, ``vbscript:`` or ``url(`` not local."""
    compact = re.sub(r"\s+", "", value).lower()
    if any(scheme in compact for scheme in ("javascript:", "data:", "vbscript:")):
        return True
    without_local = _LOCAL_URL.sub("", value.lower())
    return "url(" in re.sub(r"\s+", "", without_local)


def _style_problem(value: str) -> bool:
    lowered = value.lower()
    if any(mark in lowered for mark in ("@", "expression", "javascript:", "\\", "<")):
        return True
    return _value_problem(value)


def _attribute_problem(element: str, name: str, value: str) -> SvgProblem | None:
    namespace, local = _local(name)
    if local.lower().startswith("on"):
        return SvgProblem("attribute", local)
    if local == "href" and namespace in ("", XLINK):
        if element not in HREF_ELEMENTS or not _LOCAL_REFERENCE.fullmatch(value.strip()):
            return SvgProblem("reference", local)
        return None
    if namespace not in ("", XML):
        return SvgProblem("attribute", local)
    if namespace == XML and local not in ("space", "lang"):
        return SvgProblem("attribute", local)
    if local == "style":
        return SvgProblem("style", local) if _style_problem(value) else None
    if _value_problem(value):
        return SvgProblem("reference", local)
    return None


def _walk(root: Any) -> SvgProblem | None:
    count = 0
    stack: list[tuple[Any, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_ELEMENTS:
            return SvgProblem("too_complex")
        if not isinstance(node.tag, str):  # Comments and processing instructions are not kept by the parser.
            continue
        namespace, local = _local(node.tag)
        if namespace != SVG or local not in ELEMENTS:
            return SvgProblem("element", local)
        for name, value in node.attrib.items():
            problem = _attribute_problem(local, name, value)
            if problem is not None:
                return problem
        if local not in TEXT_ELEMENTS:
            if (node.text or "").strip():
                return SvgProblem("text", local)
            if any((child.tail or "").strip() for child in node):
                return SvgProblem("text", local)
        stack.extend((child, depth + 1) for child in reversed(list(node)))
    return None


def svg_problem(content: Any) -> SvgProblem | None:
    """The first rule the SVG breaks, or ``None``; the order of the checks is the documented one."""
    if not isinstance(content, (bytes, bytearray)) or len(content) > MAX_BYTES:
        return SvgProblem("too_large")
    text = _decode(bytes(content))
    if text is None:
        return SvgProblem("encoding")
    problem = _prolog_problem(text)
    if problem is not None:
        return problem
    try:
        # DOCTYPE, ENTITY and processing instructions were refused above, so no entity can be
        # declared or expanded; the size is bounded. That is what defusedxml would add here.
        root = ElementTree.fromstring(text)  # noqa: S314
    except ElementTree.ParseError:
        return SvgProblem("malformed")
    namespace, local = _local(root.tag)
    if namespace != SVG or local != "svg":
        return SvgProblem("not_svg", local)
    if "viewBox" not in root.attrib and not ("width" in root.attrib and "height" in root.attrib):
        return SvgProblem("not_svg", "viewBox")
    return _walk(root)
