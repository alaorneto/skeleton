"""The barrier of the free CSS of a theme (#594): pure Python, no new dependency.

Only the platform administrator writes it, and the Content Security Policy (``default-src 'self'``,
``img-src 'self' data:``, ``font-src 'self'``) already blocks every external load. This is
defence in depth: text without markup, without resource loading and well formed. The CSS is
served only as ``text/css`` from ``/brand/theme.css`` and never embedded in HTML.

The checks run on a normalized form (comments removed, CSS escapes decoded, lowercase), so
``u\\72l(``, ``\\75 rl(`` and ``@im/**/port`` are caught; the text stored is the original.
"""

from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.translation import gettext

MAX_BYTES = 32 * 1024
FORBIDDEN = (
    "@import",
    "@charset",
    "@namespace",
    "url(",
    "image-set(",
    "image(",
    "src(",
    "expression(",
    "javascript:",
    "behavior",
    "-moz-binding",
)
_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\f]?|(.))", re.DOTALL)


def _refuse(message: str) -> ValidationError:
    return ValidationError({"custom_css": [message]})


def _scan(text: str) -> str:
    """Check strings, comments and nesting; return the text without comments (strings kept)."""
    out: list[str] = []
    braces = parens = 0
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == "/" and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise _refuse(gettext("Comentário sem fim."))
            index = end + 2
            continue
        if char in "\"'":
            quote, start = char, index
            index += 1
            while index < length and text[index] != quote:
                if text[index] == "\\":
                    index += 1
                elif text[index] == "\n":
                    break
                index += 1
            if index >= length or text[index] != quote:
                raise _refuse(gettext("Texto entre aspas sem fim."))
            out.append(text[start : index + 1])
            index += 1
            continue
        if char == "\\":
            out.append(text[index : index + 2])  # An escaped character is never a delimiter.
            index += 2
            continue
        if char == "{":
            braces += 1
        elif char == "}":
            braces -= 1
        elif char == "(":
            parens += 1
        elif char == ")":
            parens -= 1
        if braces < 0 or parens < 0:
            raise _refuse(gettext("Chaves ou parênteses sem par."))
        out.append(char)
        index += 1
    if braces or parens:
        raise _refuse(gettext("Chaves ou parênteses sem par."))
    return "".join(out)


def _decode(match: re.Match[str]) -> str:
    if match.group(1) is not None:
        code = int(match.group(1), 16)
        return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "�"
    return match.group(2)


def normalized(text: str) -> str:
    """The form the forbidden list is checked against: no comments, escapes decoded, lowercase.

    Comments are removed by the same scanner that knows strings, so a ``/*`` inside a string
    never hides what follows it. Raises ``ValidationError`` for malformed text.
    """
    return _ESCAPE.sub(_decode, _scan(text)).lower()


def validate_custom_css(text: Any) -> str:
    """The original text (``\\r\\n`` → ``\\n``) or ``ValidationError({"custom_css": [...]})``."""
    if not isinstance(text, str):
        raise _refuse(gettext("Use um texto."))
    text = text.replace("\r\n", "\n")
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise _refuse(gettext("O CSS pode ter até 32 KB."))
    if "\x00" in text or "<" in text:
        raise _refuse(gettext("O CSS não pode conter < nem caracteres nulos."))
    if not text.strip():
        return ""
    form = normalized(text)
    if "\x00" in form or "<" in form:
        raise _refuse(gettext("O CSS não pode conter < nem caracteres nulos."))
    compact = re.sub(r"\s+", "", form)
    for item in FORBIDDEN:
        if item in form or item in compact:
            raise _refuse(gettext("O CSS não pode usar %(item)s.") % {"item": item})
    return text
