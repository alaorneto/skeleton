"""Compile safe, request-local CRUD projections into fixed crispy layouts."""
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Field, Layout, LayoutObject
from django.template.loader import get_template

from skeleton.web.forms import CHANGE_PREFIX
from skeleton.web.presentation import _accessible, resolve_field_span

__all__ = ["build_crud_layout", "build_crud_helper"]


class _CrudField(Field):
    def __init__(self, entry):
        self.entry = entry
        super().__init__(entry["bound"].name, template="skeleton_crud/field.html")

    def render(self, form, context, template_pack="skeleton_crud", **kwargs):
        return super().render(form, context, template_pack,
                              extra_context={"entry": self.entry}, **kwargs)


class _CrudValue(LayoutObject):
    def __init__(self, entry, *, mode):
        self.entry = entry
        self.mode = mode
        self.fields = []

    def render(self, form, context, template_pack="skeleton_crud", **kwargs):
        with context.push(entry=self.entry, mode=self.mode):
            return get_template("skeleton_crud/value.html").render(context.flatten())


class _CrudSection(LayoutObject):
    def __init__(self, section, *fields, mode):
        self.section = section
        self.fields = list(fields)
        self.mode = mode

    def render(self, form, context, template_pack="skeleton_crud", **kwargs):
        with context.push(section=self.section, mode=self.mode):
            fields_output = self.get_rendered_fields(form, context, template_pack, **kwargs)
            with context.push(fields_output=fields_output):
                return get_template("skeleton_crud/layout/section.html").render(context.flatten())


def build_crud_layout(sections, *, mode="edit"):
    if mode not in ("edit", "read"):
        raise ValueError("Invalid CRUD layout mode.")
    return Layout(*(_CrudSection(section, *(
        _CrudValue(entry, mode=mode) if mode == "read" or entry["read_only"] else _CrudField(entry)
        for entry in section["fields"]), mode=mode) for section in sections))


def _default_sections(form, visible):
    fields = []
    for bound in visible:
        if bound.name.startswith(CHANGE_PREFIX):
            continue
        entry = {"bound": bound, "label": bound.label, "help_text": bound.help_text,
                 "col_span": 12, "read_only": False}
        control = CHANGE_PREFIX + bound.name
        if control in form.fields and not form[control].is_hidden:
            entry["change_bound"] = form[control]
        fields.append(entry)
    return [{"id": "fields", "label": "Dados do registro", "tab_id": None,
             "columns": 1, "fields": fields}] if fields else []


def build_crud_helper(form, sections=None):
    visible = form.visible_fields()
    sections = sections or _default_sections(form, visible)
    expected = {bound.name for bound in visible}
    covered = set()

    def cover(bound):
        if bound.form is not form or bound.name not in expected or bound.name in covered:
            raise ValueError("Incomplete CRUD form layout.")
        covered.add(bound.name)
        _accessible(bound)

    for section in sections:
        for entry in section["fields"]:
            resolve_field_span(entry["col_span"], section["columns"])
            if entry["read_only"]:
                continue
            bound = entry["bound"]
            if bound.name.startswith(CHANGE_PREFIX):
                raise ValueError("Incomplete CRUD form layout.")
            cover(bound)
            control = entry.get("change_bound")
            if control is not None:
                if control.name != CHANGE_PREFIX + bound.name:
                    raise ValueError("Incomplete CRUD form layout.")
                cover(control)
    if covered != expected:
        raise ValueError("Incomplete CRUD form layout.")
    helper = FormHelper()
    helper.template_pack = "skeleton_crud"
    helper.layout = build_crud_layout(sections)
    helper.form_tag = False
    helper.disable_csrf = True
    helper.render_hidden_fields = False
    helper.render_unmentioned_fields = False
    helper.render_required_fields = False
    helper.include_media = False
    return helper
