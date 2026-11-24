"""Formatted text in templates (#824): ``{% load skeleton_markdown %}``.

``{{ text|markdown }}`` renders the stored Markdown as HTML in a ``<div class="markdown">``,
with every feature of the dialect; ``{{ text|markdown:"emphasis,lists" }}`` keeps only those
(use the field's own list, the one its editor offers). ``{{ text|markdown_plain }}`` is the
text without markers, for lists and summaries. The HTML comes only from
:func:`skeleton.common.markdown.render_html`, which escapes everything typed by a person.
"""

from __future__ import annotations

from django import template
from django.template.defaultfilters import linebreaksbr
from django.utils.html import escape
from django.utils.safestring import mark_safe

from skeleton.common.markdown import MarkdownTooLong, render_html, to_plain_text

register = template.Library()


def _features(argument):
    if argument is None:
        return None
    return tuple(name.strip() for name in str(argument).split(",") if name.strip())


@register.filter(name="markdown", is_safe=True)
def markdown_filter(value, features=None):
    """The HTML of the Markdown ``value``; empty for ``None`` and ``""``."""
    if value is None or value == "":
        return ""
    try:
        html = render_html(str(value), features=_features(features))
    except MarkdownTooLong:
        # A value stored before a limit existed is still shown, as escaped plain text.
        html = linebreaksbr(escape(value))
    return mark_safe(f'<div class="markdown">{html}</div>')  # noqa: S308 - render_html escapes the text


@register.filter(name="markdown_plain")
def markdown_plain_filter(value, features=None):
    """The text of the Markdown ``value`` without markers (autoescaped by the template)."""
    if value is None or value == "":
        return ""
    try:
        return to_plain_text(str(value), features=_features(features))
    except MarkdownTooLong:
        return str(value)
