"""A theme stylesheet resolved for a colour scheme the person chose (#665): pure Python, no Django.

Theme authors write the dark mode as ``@media (prefers-color-scheme: dark) { … }`` (docs/themes.md),
which follows the operating system. When the person forces ``light`` or ``dark``, the core serves
the same stylesheet with each ``(prefers-color-scheme: <value>)`` feature of a ``@media`` prelude
swapped for a fixed literal: ``(min-width:0)`` (always true) when the value is the forced one,
``(not (min-width:0))`` (always false) when it is not. Only the feature is swapped, so ``not``,
``and``, ``or`` and comma lists keep their meaning, and nothing but fixed text is ever inserted:
CSS that passed ``css_validation`` still passes it.

Comments and strings are never touched. An at-rule name written with CSS escapes (``@\\6d edia``)
is not recognised and keeps following the system: a cosmetic miss, never a security one.
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = ["SCHEMES", "force_color_scheme"]

SCHEMES = ("light", "dark")
ALWAYS = "(min-width:0)"
NEVER = "(not (min-width:0))"
_MEDIA = re.compile(r"@media(?![\w-])", re.IGNORECASE)
_FEATURE = re.compile(r"\(\s*prefers-color-scheme\s*:\s*(light|dark)\s*\)", re.IGNORECASE)


def _masked(text: str) -> str:
    """The text with comments and strings turned into spaces of the same length (offsets kept)."""
    out: list[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == "/" and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            out.append(" " * (end - index))
            index = end
            continue
        if char in "\"'":
            start = index
            index += 1
            while index < length and text[index] not in (char, "\n"):
                index += 2 if text[index] == "\\" else 1
            index = min(length, index + 1)
            out.append(" " * (index - start))
            continue
        if char == "\\":
            out.append(text[index : index + 2])  # An escaped character is never a delimiter.
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)[:length]


@lru_cache(maxsize=32)
def force_color_scheme(css: str, scheme: str) -> str:
    """``css`` with every ``prefers-color-scheme`` media feature fixed to ``scheme`` (``light``/``dark``)."""
    if scheme not in SCHEMES:
        raise ValueError(f"unknown colour scheme: {scheme!r}")
    if "prefers-color-scheme" not in css.lower():
        return css
    masked = _masked(css)
    spans: list[tuple[int, int, str]] = []
    for media in _MEDIA.finditer(masked):
        stops = [found for found in (masked.find("{", media.end()), masked.find(";", media.end())) if found >= 0]
        end = min(stops) if stops else len(masked)
        for feature in _FEATURE.finditer(masked, media.end(), end):
            spans.append((feature.start(), feature.end(), ALWAYS if feature.group(1).lower() == scheme else NEVER))
    for start, end, literal in reversed(spans):
        css = css[:start] + literal + css[end:]
    return css
