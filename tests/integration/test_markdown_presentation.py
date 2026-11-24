"""The markdown widget in the record pages (#826), in PostgreSQL with the runtime role."""

from dataclasses import replace

import pytest
from django.template.loader import render_to_string

from skeleton.system.presentation import FieldUISpec, FormLayoutSpec, FormSectionSpec, ListSpec, ModelUISpec
from skeleton.system.registry import get_spec
from skeleton.web.forms import record_form
from skeleton.web.markdown_widget import MarkdownTextarea
from skeleton.web.presentation import detail_sections, form_sections, list_context
from tools.security_fixtures import actor_tenant, build_security_fixture

BODY = "## Evolução\n\n**forte** e ++sub++\n\n- item\n\n<script>alert(1)</script> [x](javascript:alert(1)) [ok](https://exemplo.test)"


@pytest.fixture
def records(skeleton_db):
    return build_security_fixture()


def _ui(note, *, features=None, read_only=False):
    fields = tuple(
        FieldUISpec("body", widget="markdown", markdown_features=features, read_only=read_only)
        if name == "body"
        else FieldUISpec(name)
        for name in get_spec(note._meta.label_lower).write_fields
    )
    return ModelUISpec(
        note._meta.label_lower,
        list=ListSpec(columns=("title", "body")),
        form=FormLayoutSpec(sections=(FormSectionSpec("main", "Main", fields),)),
    )


def _set_ui(monkeypatch, ui):
    monkeypatch.setattr("skeleton.system.presentation.get_model_ui", lambda key: ui)
    monkeypatch.setattr("skeleton.web.presentation.get_model_ui", lambda key: ui)


@pytest.mark.needs_example_app
def test_detail_list_and_form_of_a_markdown_field(records, monkeypatch, subtests):
    from examples.notes.models import Note
    from skeleton.web.services import create_record

    spec = get_spec(Note._meta.label_lower)
    with actor_tenant(records.alice, records.a):
        note = create_record(spec, user=records.alice, data={"code": "md-1", "title": "Formatada", "body": BODY})
        assert Note.objects.get(pk=note.pk).body == BODY  # The stored value is the Markdown itself.

        with subtests.test("detail renders the server HTML and escapes what was typed"):
            _set_ui(monkeypatch, _ui(Note))
            html = render_to_string(
                "skeleton/web/detail.html", {"title": "x", **detail_sections(note, spec, user=records.alice)}
            )
            assert '<div class="markdown"><h2>Evolução</h2>' in html
            assert "<strong>forte</strong> e <u>sub</u>" in html
            assert "<li>item</li>" in html
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert" not in html
            assert 'href="javascript' not in html and "[x](javascript:alert(1))" in html
            assert '<a href="https://exemplo.test" rel="nofollow noopener noreferrer">ok</a>' in html
            assert "**forte**" not in html

        with subtests.test("the field's features restrict the detail too"):
            _set_ui(monkeypatch, _ui(Note, features=("emphasis",)))
            html = render_to_string(
                "skeleton/web/detail.html", {"title": "x", **detail_sections(note, spec, user=records.alice)}
            )
            assert "<strong>forte</strong> e ++sub++" in html and "<h2>" not in html

        with subtests.test("the list shows the text without markers"):
            _set_ui(monkeypatch, _ui(Note))
            cells = list_context(spec, [note], user=records.alice)["record_rows"][0]["cells"]
            assert cells[1]["text"].startswith("Evolução\nforte e sub\nitem\n<script>")
            assert "**" not in cells[1]["text"] and "##" not in cells[1]["text"]

        with subtests.test("the form edits with the Markdown editor"):
            _set_ui(monkeypatch, _ui(Note, features=("emphasis", "lists")))
            form = record_form(spec, user=records.alice, instance=note)
            widget = form.fields["body"].widget
            assert isinstance(widget, MarkdownTextarea) and widget.features == ("emphasis", "lists")
            html = render_to_string(
                "skeleton/web/form.html",
                {
                    "form": form,
                    "title": "x",
                    "csrf_token": "t",
                    **form_sections(form, spec, instance=note, user=records.alice),
                },
            )
            assert 'data-markdown-features="emphasis lists"' in html
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

        with subtests.test("a read-only markdown field of the form is rendered as HTML"):
            _set_ui(monkeypatch, _ui(Note, read_only=True))
            read_only = replace(spec, write_fields=tuple(name for name in spec.write_fields if name != "body"))
            form = record_form(read_only, user=records.alice, instance=note)
            sections = form_sections(form, read_only, instance=note, user=records.alice)
            html = render_to_string(
                "skeleton/web/form.html", {"form": form, "title": "x", "csrf_token": "t", **sections}
            )
            assert '<div class="read-only-value markdown"><h2>Evolução</h2>' in html
