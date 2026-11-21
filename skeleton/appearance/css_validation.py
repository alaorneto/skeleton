"""The barrier of the free CSS of a theme (#594): pure Python, no new dependency.

Only the platform administrator writes it, and the Content Security Policy (``default-src 'self'``,
``img-src 'self' data:``, ``font-src 'self'``) already blocks every external load. This is
defence in depth: text without markup, without resource loading and well formed. The CSS is
served only as ``text/css`` from ``/brand/theme.css`` and never embedded in HTML.

The checks run on a normalized form (comments blanked, CSS escapes decoded, lowercase), so
``u\\72l(``, ``\\75 rl(`` and ``@im/**/port`` are caught; the text stored is the original.

``css_problem`` is the check itself and imports nothing from Django (#631): the theme package
reader (``package_format``) also runs inside ``package build``, which works without settings.
``validate_custom_css`` wraps it for forms and services, with a message that names the line.
The line is counted on the normalized form: a hexadecimal escape followed by a line break eats
the break, so in that rare case the line may be one less than the real one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = ["FORBIDDEN", "MAX_BYTES", "CssProblem", "css_problem", "normalized", "validate_custom_css"]

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
CODES = ("not_text", "too_large", "markup", "open_comment", "open_string", "unbalanced", "forbidden")


@dataclass(frozen=True, slots=True)
class CssProblem:
    """What the free CSS breaks: one of ``CODES``, the 1-based line (0 = the whole text) and the item."""

    code: str
    line: int
    item: str = ""


class _Problem(ValueError):
    def __init__(self, code: str, index: int):
        super().__init__(code)
        self.code = code
        self.index = index


def _line(text: str, index: int) -> int:
    return text.count("\n", 0, max(0, index)) + 1


def _blank(segment: str) -> str:
    """A comment turned into spaces that keep its line breaks (the lines of what follows stay right)."""
    return "".join("\n" if char == "\n" else " " for char in segment)


def _scan(text: str) -> str:
    """Check strings, comments and nesting; return the text with comments blanked (strings kept)."""
    out: list[str] = []
    opened: dict[str, list[int]] = {"{": [], "(": []}
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == "/" and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise _Problem("open_comment", index)
            out.append(_blank(text[index : end + 2]))
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
                raise _Problem("open_string", start)
            out.append(text[start : index + 1])
            index += 1
            continue
        if char == "\\":
            out.append(text[index : index + 2])  # An escaped character is never a delimiter.
            index += 2
            continue
        # Braces and parentheses are counted apart, as before #631: only the line is new.
        if char in "{(":
            opened[char].append(index)
        elif char in "})":
            stack = opened["{" if char == "}" else "("]
            if not stack:
                raise _Problem("unbalanced", index)
            stack.pop()
        out.append(char)
        index += 1
    left = [stack[-1] for stack in opened.values() if stack]
    if left:
        raise _Problem("unbalanced", max(left))
    return "".join(out)


def _decode(match: re.Match[str]) -> str:
    if match.group(1) is not None:
        code = int(match.group(1), 16)
        return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "�"
    return match.group(2)


def normalized(text: str) -> str:
    """The form the forbidden list is checked against: comments blanked, escapes decoded, lowercase.

    Comments are blanked by the same scanner that knows strings, so a ``/*`` inside a string
    never hides what follows it. Raises ``ValueError`` for malformed text.
    """
    return _ESCAPE.sub(_decode, _scan(text)).lower()


def _forbidden(form: str) -> CssProblem | None:
    for item in FORBIDDEN:
        found = form.find(item)
        if found >= 0:
            return CssProblem("forbidden", _line(form, found), item)
    compact = re.sub(r"\s+", "", form)
    for item in FORBIDDEN:
        if item in compact:
            # Split by blanked comments or spaces: the first line whose compact form, joined with
            # the following lines, starts holding the item.
            lines = form.split("\n")
            for number in range(len(lines), 0, -1):
                if item in re.sub(r"\s+", "", "\n".join(lines[number - 1 :])):
                    return CssProblem("forbidden", number, item)
            return CssProblem("forbidden", 1, item)
    return None


def css_problem(text: Any) -> CssProblem | None:
    """The first rule the free CSS breaks, or ``None``; never imports Django."""
    if not isinstance(text, str):
        return CssProblem("not_text", 0)
    text = text.replace("\r\n", "\n")
    if len(text.encode("utf-8")) > MAX_BYTES:
        return CssProblem("too_large", 0)
    marks = [found for found in (text.find("\x00"), text.find("<")) if found >= 0]
    if marks:
        return CssProblem("markup", _line(text, min(marks)))
    if not text.strip():
        return None
    try:
        blanked = _scan(text)
    except _Problem as problem:
        return CssProblem(problem.code, _line(text, problem.index))
    form = _ESCAPE.sub(_decode, blanked).lower()
    marks = [found for found in (form.find("\x00"), form.find("<")) if found >= 0]
    if marks:
        return CssProblem("markup", _line(form, min(marks)))
    return _forbidden(form)


def _message(problem: CssProblem) -> str:
    from django.utils.translation import gettext

    messages = {
        "not_text": gettext("Use um texto."),
        "too_large": gettext("O CSS pode ter até 32 KB."),
        "markup": gettext("Linha %(line)s: o CSS não pode conter < nem caracteres nulos, nem em comentários."),
        "open_comment": gettext("Linha %(line)s: comentário sem fim."),
        "open_string": gettext("Linha %(line)s: texto entre aspas sem fim."),
        "unbalanced": gettext("Linha %(line)s: chaves ou parênteses sem par."),
        "forbidden": gettext("Linha %(line)s: o CSS não pode usar %(item)s."),
    }
    return messages[problem.code] % {"line": problem.line, "item": problem.item}


def css_problem_message(problem: CssProblem) -> str:
    """The Portuguese sentence of a problem (needs Django for ``gettext``)."""
    return _message(problem)


def validate_custom_css(text: Any) -> str:
    """The original text (``\\r\\n`` → ``\\n``) or ``ValidationError({"custom_css": [...]})``."""
    from django.core.exceptions import ValidationError

    problem = css_problem(text)
    if problem is not None:
        raise ValidationError({"custom_css": [_message(problem)]})
    text = text.replace("\r\n", "\n")
    return text if text.strip() else ""
