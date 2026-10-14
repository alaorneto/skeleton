"""Real-record regression coverage for the generic presentation boundary."""
from dataclasses import replace
from html.parser import HTMLParser

from django import forms

import pytest
from django.db import connection
from django.template.loader import render_to_string
from django.test.utils import CaptureQueriesContext

from skeleton.system.presentation import FieldUISpec, FormLayoutSpec, FormSectionSpec, ListSpec, ModelUISpec, TabSpec
from skeleton.system.registry import get_spec
from skeleton.web.forms import record_form, wire_values
from skeleton.web.presentation import detail_sections, form_sections, list_context, list_fields
from tools.security_fixtures import actor_tenant, build_security_fixture


@pytest.fixture
def records(skeleton_db):
    return build_security_fixture()


def set_ui(monkeypatch, ui):
    monkeypatch.setattr("skeleton.system.presentation.get_model_ui", lambda key: ui)
    monkeypatch.setattr("skeleton.web.presentation.get_model_ui", lambda key: ui)



class RenderedHTML(HTMLParser):
    """Inspect server-rendered DOM without depending on attribute ordering."""

    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def named(self, name):
        return [attrs for _, attrs in self.elements if attrs.get("name") == name]

    def identified(self, identifier):
        return next(attrs for _, attrs in self.elements if attrs.get("id") == identifier)


def render_form(form, **context):
    return render_to_string("skeleton/web/form.html", {
        "form": form, "title": "Record", "csrf_token": "renderer-regression-token", **context,
    })

def test_write_only_never_reads_current_value_and_skip_differs_from_clear(records):
    from examples.notes.models import Note
    from skeleton.web.services import update_record

    spec = replace(get_spec(Note._meta.label_lower), read_fields=("code", "title"))
    with actor_tenant(records.alice, records.a):
        item = Note.objects.get(pk=records.private.pk)
        form = record_form(spec, user=records.alice, instance=item)
        html = render_form(form, **form_sections(form, spec, instance=item, user=records.alice))
        assert item.body not in html
        dom = RenderedHTML(html)
        assert len(dom.named("__change_body")) == 1
        assert len(dom.named("revision")) == 1
        assert len(dom.named("csrfmiddlewaretoken")) == 1
        assert form["body"].value() in (None, "")
        submitted = {"code": item.code, "title": item.title, "revision": str(item.revision),
                     "body": "forged without opt-in"}
        unchanged = record_form(spec, user=records.alice, instance=item, data=submitted)
        assert unchanged.is_valid(), unchanged.errors
        assert "body" not in wire_values(unchanged.cleaned_data)
        assert "revision" not in wire_values(unchanged.cleaned_data)
        update_record(item, spec, user=records.alice, data=wire_values(unchanged.cleaned_data))
        item.refresh_from_db()
        assert item.body == "secret body private"
        preserved_form = record_form(spec, user=records.alice, instance=item)
        preserved_html = render_form(preserved_form, **form_sections(
            preserved_form, spec, instance=item, user=records.alice))
        assert item.body not in preserved_html
        submitted.update(revision=str(item.revision), body="", __change_body="on")
        cleared = record_form(spec, user=records.alice, instance=item, data=submitted)
        assert cleared.is_valid(), cleared.errors
        assert wire_values(cleared.cleaned_data)["body"] == ""
        update_record(item, spec, user=records.alice, data=wire_values(cleared.cleaned_data))
        item.refresh_from_db()
        assert item.body == ""
        clear_form = record_form(spec, user=records.alice, instance=item)
        clear_html = render_form(clear_form, **form_sections(
            clear_form, spec, instance=item, user=records.alice))
        assert "secret body private" not in clear_html
        assert len(RenderedHTML(clear_html).named("__change_body")) == 1
        assert not any(name.startswith("__change_") for name in wire_values(cleared.cleaned_data))


def test_write_only_skip_bypasses_required_validation_but_change_does_not(records):
    from examples.notes.models import Note

    spec = replace(get_spec(Note._meta.label_lower), read_fields=("code", "body"))
    with actor_tenant(records.alice, records.a):
        data = {"code": records.private.code, "body": "", "revision": str(records.private.revision)}
        skipped = record_form(spec, user=records.alice, instance=records.private, data=data)
        assert skipped.is_valid(), skipped.errors
        assert "title" not in wire_values(skipped.cleaned_data)
        data["__change_title"] = "on"
        changed = record_form(spec, user=records.alice, instance=records.private, data=data)
        assert not changed.is_valid()
        assert "title" in changed.errors
        # Requiredness remains server-side so unchecking the change control
        # after an error also works without JavaScript.
        assert "required" not in str(changed["title"])
        created = record_form(spec, user=records.alice, data={"code": "new", "body": ""})
        assert not created.is_valid()
        assert "title" in created.errors


def test_layout_validation_preserves_submitted_values_and_activates_invalid_tab(records, monkeypatch):
    from examples.notes.models import Note

    spec = get_spec(Note._meta.label_lower)
    ui = ModelUISpec(Note._meta.label_lower, form=FormLayoutSpec(
        sections=(FormSectionSpec("identity", "Identity", (
                      FieldUISpec("code", col_span=4), FieldUISpec("title", col_span=8)), "general", 2),
                  FormSectionSpec("content", "Content", (
                      FieldUISpec("body", widget="textarea", col_span=12, rows=8),), "content")),
        tabs=(TabSpec("general", "General"), TabSpec("content", "Content"))))
    set_ui(monkeypatch, ui)
    with actor_tenant(records.alice, records.a):
        form = record_form(spec, user=records.alice, data={"code": "new", "title": "Retained title", "body": "x" * 10001})
        assert not form.is_valid()
        assert "body" in form.errors
        assert form["title"].value() == "Retained title"
        context = form_sections(form, spec, user=records.alice, operation="create")
        assert context["active_tab"] == "content"
        assert context["sections"][0]["columns"] == 2
        assert form.fields["body"].widget.attrs["aria-invalid"] == "true"
        html = render_form(form, **context)
        # Both sections exist in the server response; JS, not server markup,
        # hides inactive panels so every field remains usable without JS.
        assert 'id="id_body"' in html and 'id="id_title"' in html
        assert 'href="#id_body"' in html
        assert 'id="id_body_error"' in html
        dom = RenderedHTML(html)
        assert dom.identified("id_title")["value"] == "Retained title"
        assert dom.identified("id_body")["aria-invalid"] == "true"
        assert "id_body_error" in dom.identified("id_body")["aria-describedby"].split()
        assert dom.identified("id_body")["rows"] == "8"
        assert any(attrs.get("data-active-tab") == "content" for _, attrs in dom.elements)
        assert html.index('id="id_code"') < html.index('id="id_title"') < html.index('id="id_body"')
        for span in (4, 8, 12):
            assert any(f"field-span-{span}" in attrs.get("class", "").split()
                       for _, attrs in dom.elements)
        assert len(dom.named("csrfmiddlewaretoken")) == 1


def test_projection_uses_only_readable_values_and_never_business_str(records, monkeypatch):
    from examples.notes.models import Note

    monkeypatch.setattr(Note, "__str__", lambda self: pytest.fail("Business __str__ must not run"))
    spec = replace(get_spec(Note._meta.label_lower), read_fields=("code", "title"))
    ui = ModelUISpec(Note._meta.label_lower)
    set_ui(monkeypatch, ui)
    with actor_tenant(records.alice, records.a):
        listing = list_context(spec, [records.private], user=records.alice)
        assert listing["record_rows"][0]["label"] == "Private"
        detail = detail_sections(records.private, spec, user=records.alice)
        rendered = render_to_string("skeleton/web/detail.html", {"title": "Private", **detail})
        assert "secret body private" not in rendered
        assert "Private" in rendered


def test_declared_list_column_order_exact_filters_and_title_are_respected(records, monkeypatch):
    from examples.notes.models import Note

    spec = get_spec(Note._meta.label_lower)
    ui = ModelUISpec(Note._meta.label_lower,
                    list=ListSpec(columns=("title", "code"), filters=("code",), title_field="code"),
                    form=FormLayoutSpec(sections=(FormSectionSpec("fields", "Fields", (
                        FieldUISpec("title", label="Custom title"), FieldUISpec("code"),
                        FieldUISpec("body"))),)))
    set_ui(monkeypatch, ui)
    with actor_tenant(records.alice, records.a):
        context = list_context(spec, [records.private], user=records.alice,
                               params={"code": records.private.code, "owner": str(records.alice.pk)})
        assert context["headings"][0] == "Custom title"
        assert context["record_rows"][0]["cells"] == [records.private.title, records.private.code]
        assert context["record_rows"][0]["label"] == records.private.code
        assert [item["name"] for item in context["filters"]] == ["code"]
        assert context["filters"][0]["value"] == records.private.code



def test_database_page_loads_projection_dependencies_without_per_row_queries(records):
    from examples.notes.models import Note
    from skeleton.web.services import create_record, record_queryset

    spec = get_spec(Note._meta.label_lower)
    with actor_tenant(records.alice, records.a):
        for number in range(24):
            create_record(spec, user=records.alice,
                          data={"code": f"page-{number}", "title": f"Page {number}", "body": "Not a list column"})
        queryset = record_queryset(spec, user=records.alice).order_by("created_at", "sysid").only(*list_fields(spec))
        with CaptureQueriesContext(connection) as single_queries:
            list_context(spec, queryset[:1], user=records.alice)
        with CaptureQueriesContext(connection) as page_queries:
            context = list_context(spec, queryset[:25], user=records.alice)
        assert len(context["record_rows"]) == 25
        assert len(page_queries) <= len(single_queries) + 1
        assert "Not a list column" not in str(context["record_rows"])


def test_defaults_readonly_fields_and_dedicated_modes(records, monkeypatch):
    from examples.notes.models import Note

    spec = get_spec(Note._meta.label_lower)
    set_ui(monkeypatch, ModelUISpec(Note._meta.label_lower))
    with actor_tenant(records.alice, records.a):
        form = record_form(spec, user=records.alice, instance=records.private)
        ui = ModelUISpec(Note._meta.label_lower, form=FormLayoutSpec(sections=(FormSectionSpec(
            "all", "All", tuple(FieldUISpec(name) for name in spec.write_fields) + (FieldUISpec("sysid", read_only=True),)),)))
        set_ui(monkeypatch, ui)
        context = form_sections(form, spec, instance=records.private, user=records.alice)
        readonly = context["sections"][0]["fields"][-1]
        assert readonly["read_only"] is True and readonly["value"] == str(records.private.pk)
        assert "sysid" not in form.fields
        html = render_form(form, **context)
        assert str(records.private.pk) in html
        assert not RenderedHTML(html).named("sysid")
        invalid = record_form(spec, user=records.alice, data={"code": "x" * 65, "title": "ok", "body": "", "owner": str(records.alice.pk)})
        assert not invalid.is_valid()
        assert "code" in invalid.errors and invalid.non_field_errors()




def test_custom_dimensions_and_literal_metadata_are_isolated_in_edit_and_detail(records, monkeypatch):
    from examples.notes.models import Note

    spec = get_spec(Note._meta.label_lower)
    literal = "{{ user.email }} <script>alert('metadata')</script>"
    first_ui = ModelUISpec(Note._meta.label_lower, form=FormLayoutSpec(sections=(
        FormSectionSpec("identity", literal, (
            FieldUISpec("code", label=literal, help_text=literal, col_span=4),
            FieldUISpec("title", label="First title", col_span=8)), columns=2),
        FormSectionSpec("content", "Content", (
            FieldUISpec("body", widget="textarea", col_span=12, rows=8),)),
    )))
    second_ui = ModelUISpec(Note._meta.label_lower, form=FormLayoutSpec(sections=(
        FormSectionSpec("different", "Different", (
            FieldUISpec("title", label="Second title", col_span=12),
            FieldUISpec("code", label="Second code", col_span=12),
            FieldUISpec("body", widget="textarea", rows=3, col_span=12))),
    )))
    with actor_tenant(records.alice, records.a):
        set_ui(monkeypatch, first_ui)
        first_form = record_form(spec, user=records.alice, instance=records.private)
        first_context = form_sections(first_form, spec, instance=records.private, user=records.alice)
        first_detail = detail_sections(records.private, spec, user=records.alice)
        set_ui(monkeypatch, second_ui)
        second_form = record_form(spec, user=records.alice, instance=records.private)
        second_context = form_sections(second_form, spec, instance=records.private, user=records.alice)
        second_html = render_form(second_form, **second_context)
        # Render the first instance after constructing and rendering the second.
        first_html = render_form(first_form, user=records.alice, **first_context)
        detail_html = render_to_string("skeleton/web/detail.html", {
            "title": "Read record", "user": records.alice, **first_detail,
        })
    for html in (first_html, detail_html):
        assert "{{ user.email }}" in html
        assert "&lt;script&gt;" in html and "&lt;/script&gt;" in html
        assert "<script>alert(" not in html
        assert "First title" in html and "Second title" not in html
        assert html.index("field-span-4") < html.index("field-span-8")
    assert "Second title" in second_html and "First title" not in second_html
    assert RenderedHTML(first_html).identified("id_body")["rows"] == "8"
    assert RenderedHTML(second_html).identified("id_body")["rows"] == "3"
    assert first_html.index('id="id_code"') < first_html.index('id="id_title"')
    assert second_html.index('id="id_title"') < second_html.index('id="id_code"')
    assert not any(tag in ("input", "textarea", "select", "form")
                   for tag, _ in RenderedHTML(detail_html).elements)
    assert records.private.title in detail_html


@pytest.mark.parametrize("defect", ("omit-field", "duplicate-field", "omit-control", "duplicate-control"))
def test_explicit_layout_rejects_incomplete_or_duplicated_rendered_controls(defect):
    from skeleton.web.form_engine import build_crud_helper

    form = forms.Form()
    form.fields["title"] = forms.CharField()
    form.fields["body"] = forms.CharField(widget=forms.Textarea, required=False)
    form.fields["__change_body"] = forms.BooleanField(required=False)
    title = {"bound": form["title"], "label": "Title", "help_text": "",
             "read_only": False, "col_span": 12}
    body = {"bound": form["body"], "change_bound": form["__change_body"],
            "label": "Body", "help_text": "", "read_only": False, "col_span": 12}
    entries = [title, body]
    if defect == "omit-field":
        entries = [body]
    elif defect == "duplicate-field":
        entries.append(dict(title))
    elif defect == "omit-control":
        body.pop("change_bound")
    else:
        title["change_bound"] = form["__change_body"]
    sections = [{"id": "fields", "label": "Fields", "columns": 1,
                 "tab_id": None, "fields": entries}]
    with pytest.raises(ValueError, match=r"^Incomplete CRUD form layout\.$"):
        build_crud_helper(form, sections)


@pytest.mark.parametrize("sections", (None, []))
def test_default_layout_pairs_auxiliary_controls_and_renders_each_once(sections):
    form = forms.Form()
    form.fields["title"] = forms.CharField(label="Default title", help_text="Title help")
    form.fields["body"] = forms.CharField(widget=forms.Textarea, required=False)
    form.fields["__change_body"] = forms.BooleanField(required=False)
    form.fields["revision"] = forms.IntegerField(widget=forms.HiddenInput, initial=7)
    html = render_form(form, **({"sections": sections} if sections is not None else {}))
    dom = RenderedHTML(html)
    for name in ("title", "body", "__change_body", "revision", "csrfmiddlewaretoken"):
        assert len(dom.named(name)) == 1
    assert "Default title" in html and "Title help" in html
    assert "id_title_helptext" in dom.identified("id_title")["aria-describedby"].split()
    assert sum("change-control" in attrs.get("class", "").split()
               for _, attrs in dom.elements) == 1


@pytest.mark.parametrize("operation", ("delete", "restore"))
def test_confirmation_empty_layout_renders_only_hidden_revision_and_csrf(operation):
    form = forms.Form()
    form.fields["revision"] = forms.IntegerField(widget=forms.HiddenInput, initial=7)
    html = render_form(form, sections=[], operation=operation, confirmation="Confirm record action")
    dom = RenderedHTML(html)
    assert len(dom.named("revision")) == 1
    assert len(dom.named("csrfmiddlewaretoken")) == 1
    assert not any(tag in ("textarea", "select") or
                   (tag == "input" and attrs.get("type") != "hidden")
                   for tag, attrs in dom.elements)
    assert not any("field" in attrs.get("class", "").split() for _, attrs in dom.elements)
