"""The closed Markdown dialect of formatted text fields (#824). Free of Django.

A formatted field stores Markdown, never HTML: the browser editor (``frontend/src/markdown-editor``)
and this module read it with the same parser family (``markdown-it`` there, ``markdown-it-py``
here) and the same rules, so what the editor shows is what every read-only page renders.

The dialect is CommonMark restricted to named features. Raw HTML, images, tables, code blocks,
setext headings, autolinks and link references never exist: whatever looks like them stays text,
and every ``<`` or ``&`` typed by a person leaves escaped. A link opens only ``https://`` or a
local path; any other target keeps its label as plain text. The output of :func:`render_html`
is therefore safe to place in a page as is.
"""

from __future__ import annotations

from functools import cache
from typing import Literal
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline.state_inline import Delimiter, StateInline
from markdown_it.token import Token

__all__ = [
    "FEATURES",
    "MAX_SOURCE_LENGTH",
    "MarkdownError",
    "MarkdownTooLong",
    "render_html",
    "to_plain_text",
    "validate_features",
]

# The order is the order of the editor's toolbar.
FEATURES = ("emphasis", "underline", "strike", "code", "heading", "lists", "quote", "link")
MAX_SOURCE_LENGTH = 100_000
MAX_HEADING_LEVEL = 3
LINK_REL = "nofollow noopener noreferrer"
_MAX_NESTING = 20
_FEATURE_RULES = {
    "emphasis": ("emphasis",),
    "underline": ("underline",),
    "strike": ("strikethrough",),
    "code": ("backticks",),
    "heading": ("heading",),
    "lists": ("list",),
    "quote": ("blockquote",),
    "link": ("link",),
}
# Always on: paragraphs, text, line breaks, backslash escapes and entities.
_BASE_RULES = ("paragraph", "text", "newline", "escape", "entity")
_PLUS = 0x2B


class MarkdownError(ValueError):
    """The text or the requested features are not accepted by the dialect."""


class MarkdownTooLong(MarkdownError):
    """The text is longer than :data:`MAX_SOURCE_LENGTH`."""


def validate_features(features=None) -> tuple[str, ...]:
    """Return the canonical tuple of features (in :data:`FEATURES` order); ``None`` means all."""
    if features is None:
        return FEATURES
    if isinstance(features, str):
        raise MarkdownError("Markdown features must be a sequence of names, not a string.")
    names = tuple(features)
    unknown = sorted(set(names) - set(FEATURES))
    if unknown:
        raise MarkdownError("Unknown Markdown feature: " + ", ".join(unknown) + ".")
    if len(set(names)) != len(names):
        raise MarkdownError("Repeated Markdown feature.")
    return tuple(name for name in FEATURES if name in names)


def render_html(text: str, *, features=None) -> str:
    """Render ``text`` as escaped HTML with only the given features (default: all)."""
    return _parser(validate_features(features)).render(_source(text))


def to_plain_text(text: str, *, features=None) -> str:
    """Return the text without Markdown markers: one line per block, for lists and summaries."""
    md = _parser(validate_features(features))
    lines = []
    for token in md.parse(_source(text)):
        if token.type != "inline":
            continue
        parts = []
        for child in token.children or ():
            if child.type in ("text", "code_inline"):
                parts.append(child.content)
            elif child.type in ("softbreak", "hardbreak"):
                parts.append(" ")
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _source(text) -> str:
    if not isinstance(text, str):
        raise MarkdownError("Markdown text must be a string.")
    if len(text) > MAX_SOURCE_LENGTH:
        raise MarkdownTooLong(f"Markdown text is limited to {MAX_SOURCE_LENGTH} characters.")
    return text


def is_allowed_link(url: str) -> bool:
    """``https://`` with a host, or a local path (``/…``, never ``//`` nor a backslash)."""
    if any(ord(character) < 0x20 or character == "\\" for character in url):
        return False
    if url.startswith("/"):
        return not url.startswith("//")
    if url[:8].lower() != "https://":
        return False
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


class _DialectMarkdown(MarkdownIt):
    def validateLink(self, url: str) -> bool:  # noqa: N802 - markdown-it's name
        return is_allowed_link(url)


@cache
def _parser(features: tuple[str, ...]) -> MarkdownIt:
    md = _DialectMarkdown("zero", {"html": False, "xhtmlOut": False, "breaks": True, "linkify": False})
    md.options["maxNesting"] = _MAX_NESTING
    md.inline.ruler.before("emphasis", "underline", _underline_tokenize)
    md.inline.ruler2.before("emphasis", "underline", _underline_post_process)
    md.core.ruler.push("skeleton_dialect", _dialect_core_rule)
    rules = list(_BASE_RULES)
    for name in features:
        rules.extend(_FEATURE_RULES[name])
    md.disable("underline")  # A rule added to a chain starts enabled; only the features turn it on.
    md.enable(rules)
    return md


def _dialect_core_rule(state) -> None:
    """Clamp heading levels and drop the markup of links whose target was refused."""
    for token in state.tokens:
        if token.type in ("heading_open", "heading_close") and int(token.tag[1:]) > MAX_HEADING_LEVEL:
            token.tag = f"h{MAX_HEADING_LEVEL}"
        if token.type == "inline" and token.children:
            token.children = _filter_links(token.children)


def _filter_links(children: list[Token]) -> list[Token]:
    kept = []
    refused = []
    for child in children:
        if child.type == "link_open":
            allowed = is_allowed_link(str(child.attrGet("href") or ""))
            refused.append(not allowed)
            if not allowed:
                continue
            child.attrSet("rel", LINK_REL)
        elif child.type == "link_close":
            if refused.pop():
                continue
        kept.append(child)
    return kept


# ++underline++: the delimiter rules of CommonMark emphasis, as in markdown-it-ins, but
# rendered as <u> (the same rule is written in frontend/src/markdown-editor/dialect.js).
def _underline_tokenize(state: StateInline, silent: bool) -> bool:
    start = state.pos
    if silent or state.src[start] != "+":
        return False
    scanned = state.scanDelims(start, True)
    length = scanned.length
    if length < 2:
        return False
    if length % 2:
        token = state.push("text", "", 0)
        token.content = "+"
        length -= 1
    for _ in range(0, length, 2):
        token = state.push("text", "", 0)
        token.content = "++"
        state.delimiters.append(
            Delimiter(
                marker=_PLUS,
                length=0,
                token=len(state.tokens) - 1,
                end=-1,
                open=scanned.can_open,
                close=scanned.can_close,
            )
        )
    state.pos += scanned.length
    return True


def _underline_token(token: Token, kind: str, nesting: Literal[1, -1]) -> None:
    token.type = kind
    token.tag = "u"
    token.nesting = nesting
    token.markup = "++"
    token.content = ""


def _underline_pairs(state: StateInline, delimiters: list[Delimiter]) -> None:
    lone_markers = []
    for start in delimiters:
        if start.marker != _PLUS or start.end == -1:
            continue
        end = delimiters[start.end]
        _underline_token(state.tokens[start.token], "u_open", 1)
        _underline_token(state.tokens[end.token], "u_close", -1)
        previous = state.tokens[end.token - 1]
        if previous.type == "text" and previous.content == "+":
            lone_markers.append(end.token - 1)
    # An odd run splits as "+" + "++" + "++": move the lone marker after the closing tags.
    while lone_markers:
        index = lone_markers.pop()
        target = index + 1
        while target < len(state.tokens) and state.tokens[target].type == "u_close":
            target += 1
        target -= 1
        if index != target:
            state.tokens[index], state.tokens[target] = state.tokens[target], state.tokens[index]


def _underline_post_process(state: StateInline) -> None:
    _underline_pairs(state, state.delimiters)
    for meta in state.tokens_meta:
        if meta and "delimiters" in meta:
            _underline_pairs(state, meta["delimiters"])
