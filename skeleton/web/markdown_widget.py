"""The form widget of formatted text (#825): a textarea that the Markdown editor enhances.

``MarkdownTextarea(features=...)`` renders a plain ``<textarea>`` (the field without JavaScript)
plus, in a ``<template>``, the editor's toolbar, mode tabs and link dialog with the texts already
translated, and the module script ``skeleton/web/markdown_editor.js`` (built by ``frontend/``).
Only the buttons of the field's features are rendered; the server renders the stored value with
the same list (``skeleton.common.markdown.render_html``).
"""

from __future__ import annotations

from dataclasses import dataclass

from django import forms
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from skeleton.common.markdown import validate_features

__all__ = ["TOOL_GROUPS", "MarkdownTextarea", "Tool"]


@dataclass(frozen=True)
class Tool:
    command: str
    feature: str
    label: str | Promise
    shortcut: str
    icon: str


# The groups of the toolbar. ``command`` is the key of COMMANDS in
# frontend/src/markdown-editor/commands.js (tests/unit/test_markdown_widget.py checks both).
TOOL_GROUPS: tuple[tuple[Tool, ...], ...] = (
    (
        Tool("bold", "emphasis", _("Negrito"), "Ctrl+B", "format-bold"),
        Tool("italic", "emphasis", _("Itálico"), "Ctrl+I", "format-italic"),
        Tool("underline", "underline", _("Sublinhado"), "Ctrl+U", "format-underline"),
        Tool("strike", "strike", _("Tachado"), "Ctrl+Shift+X", "format-strike"),
        Tool("code", "code", _("Código"), "Ctrl+E", "format-code"),
    ),
    (Tool("heading", "heading", _("Título"), "Ctrl+Alt+2", "format-heading"),),
    (
        Tool("bullet_list", "lists", _("Lista com marcadores"), "Ctrl+Shift+8", "format-list-bullet"),
        Tool("ordered_list", "lists", _("Lista numerada"), "Ctrl+Shift+7", "format-list-number"),
        Tool("quote", "quote", _("Citação"), "Ctrl+Shift+9", "format-quote"),
    ),
    (Tool("link", "link", _("Link"), "Ctrl+K", "format-link"),),
)


class MarkdownTextarea(forms.Textarea):
    """A textarea for Markdown of the closed dialect, with the editor of formatted text."""

    template_name = "skeleton/web/widgets/markdown_textarea.html"

    def __init__(self, attrs=None, *, features=None):
        self.features = validate_features(features)
        super().__init__(attrs)

    def __deepcopy__(self, memo):
        copy = super().__deepcopy__(memo)
        copy.features = self.features
        return copy

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["attrs"]["data-markdown-editor"] = ""
        context["widget"]["attrs"]["data-markdown-features"] = " ".join(self.features)
        enabled = set(self.features)
        context["widget"]["markdown_tools"] = [
            group for group in ([tool for tool in tools if tool.feature in enabled] for tools in TOOL_GROUPS) if group
        ]
        context["widget"]["markdown_link"] = "link" in enabled
        return context
