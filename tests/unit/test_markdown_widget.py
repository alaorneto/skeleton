"""The widget of formatted text (#825) and the markdown widget of the declarative UI (#826)."""

import copy
import re
from pathlib import Path

import pytest
from django import forms

from skeleton.common.markdown import FEATURES, MarkdownError
from skeleton.system.packages import PackageError, validate_manifest
from skeleton.system.presentation import (
    MARKDOWN_WIDGET_CORE_MINIMUM,
    WIDGETS,
    FieldUISpec,
    presentation_from_runtime,
    required_presentation_core,
    validate_runtime_presentation,
)
from skeleton.web.markdown_widget import TOOL_GROUPS, MarkdownTextarea
from tests.unit.test_presentation import manifest_v2

ROOT = Path(__file__).resolve().parents[2]
COMMANDS_JS = ROOT / "frontend" / "src" / "markdown-editor" / "commands.js"


class NoteForm(forms.Form):
    body = forms.CharField(required=True, widget=MarkdownTextarea(attrs={"rows": 6}))
    summary = forms.CharField(required=False, widget=MarkdownTextarea(features=["emphasis", "lists"]))


def _commands_in_js():
    """``id: { feature: "..." }`` of the COMMANDS registry of the browser editor."""
    source = COMMANDS_JS.read_text(encoding="utf-8")
    registry = source[source.index("export const COMMANDS = {") :]
    return dict(re.findall(r"^  (\w+): \{\s*feature: \"(\w+)\"", registry, flags=re.MULTILINE))


def test_toolbar_matches_the_editor_commands():
    tools = {tool.command: tool.feature for group in TOOL_GROUPS for tool in group}
    assert tools == _commands_in_js()
    assert set(tools.values()) == set(FEATURES)
    icons = [tool.icon for group in TOOL_GROUPS for tool in group]
    assert len(icons) == len(set(icons))


def test_widget_renders_textarea_chrome_and_module_script():
    html = str(NoteForm(initial={"body": "**a** <b>"})["body"])
    assert re.search(r"<textarea [^>]*data-markdown-editor[^>]*>", html)
    assert f'data-markdown-features="{" ".join(FEATURES)}"' in html
    assert "**a** &lt;b&gt;</textarea>" in html  # The value is escaped like any textarea.
    assert '<template data-markdown-chrome="id_body">' in html
    assert re.findall(r'data-command="(\w+)"', html) == [tool.command for group in TOOL_GROUPS for tool in group]
    assert "data-markdown-link-dialog" in html
    assert re.search(r'<script type="module" src="[^"]*skeleton/web/markdown_editor\.[^"]*js"></script>', html)
    assert "required" in html and 'rows="6"' in html
    assert "<script>" not in html and " style=" not in html


def test_widget_renders_only_the_buttons_of_its_features():
    html = str(NoteForm()["summary"])
    assert re.findall(r'data-command="(\w+)"', html) == ["bold", "italic", "bullet_list", "ordered_list"]
    assert 'data-markdown-features="emphasis lists"' in html
    assert "data-markdown-link-dialog" not in html


def test_widget_posts_the_markdown_and_survives_form_copies():
    form = NoteForm(data={"body": "- **a**\n- b", "summary": ""})
    assert form.is_valid()
    assert form.cleaned_data["body"] == "- **a**\n- b"
    assert copy.deepcopy(NoteForm.base_fields["summary"].widget).features == ("emphasis", "lists")
    assert not NoteForm(data={"body": "", "summary": ""}).is_valid()


def test_widget_refuses_unknown_features():
    with pytest.raises(MarkdownError):
        MarkdownTextarea(features=["tables"])


# ------------------------------------------------------------- declarative UI (#826)


def _runtime(**field):
    runtime = manifest_v2()["runtime"]
    runtime["model_ui"][0]["form"]["sections"][0]["fields"][0].update(field)
    return runtime


def test_markdown_widget_and_features_round_trip():
    assert "markdown" in WIDGETS
    runtime = _runtime(widget="markdown", rows=8, markdown_features=["lists", "emphasis"])
    validate_runtime_presentation(runtime, "contract_ui")
    _, model_ui = presentation_from_runtime(runtime)
    assert model_ui[0].form.sections[0].fields == (
        FieldUISpec("title", widget="markdown", rows=8, markdown_features=("lists", "emphasis")),
    )
    assert required_presentation_core(runtime) == MARKDOWN_WIDGET_CORE_MINIMUM
    runtime = _runtime(widget="markdown", markdown_features=None)
    validate_runtime_presentation(runtime, "contract_ui")


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ({"widget": "textarea", "markdown_features": ["lists"]}, "requires the markdown widget"),
        ({"widget": "markdown", "markdown_features": []}, "non-empty list"),
        ({"widget": "markdown", "markdown_features": ["tables"]}, "non-empty list"),
        ({"widget": "markdown", "markdown_features": "lists"}, "non-empty list"),
        ({"widget": "markdown", "markdown_features": ["lists", "lists"]}, "Duplicate Markdown feature"),
    ],
)
def test_markdown_features_are_closed(field, message):
    with pytest.raises(ValueError, match=message):
        validate_runtime_presentation(_runtime(**field), "contract_ui")


def test_manifest_with_the_markdown_widget_needs_the_core_floor():
    manifest = manifest_v2()
    manifest["runtime"]["model_ui"][0]["form"]["sections"][0]["fields"][0]["widget"] = "markdown"
    manifest["compatibility"]["skeleton"] = ">=0.8.0,<1.0.0"
    with pytest.raises(
        PackageError, match="markdown widget requires a core minimum of " + MARKDOWN_WIDGET_CORE_MINIMUM
    ):
        validate_manifest(manifest)
    manifest["compatibility"]["skeleton"] = f">={MARKDOWN_WIDGET_CORE_MINIMUM},<1.0.0"
    validate_manifest(manifest)


def test_markdown_widget_fits_only_text_fields():
    from django.db import models

    from skeleton.system.presentation import _widget_allowed

    assert _widget_allowed(models.TextField(), "markdown")
    assert _widget_allowed(models.CharField(max_length=10), "markdown")
    assert not _widget_allowed(models.IntegerField(), "markdown")
    assert not _widget_allowed(models.JSONField(), "markdown")
