"""Request-local, allowlisted projections shared by the standard record pages."""
import json
from datetime import date, datetime, time
from urllib.parse import urlencode

from django import forms
from django.apps import apps
from django.urls import reverse
from django.utils import formats, timezone

from skeleton.system.app_registry import get_app
from skeleton.system.presentation import FieldUISpec, FormSectionSpec, SYSTEM_READ_FIELDS, get_model_ui
from skeleton.tenancy.context import current_context
from skeleton.web.forms import CHANGE_PREFIX
from skeleton.web.services import SYSTEM_FIELDS, records_data


UNAVAILABLE = "Referência indisponível"


def _title_field(spec):
    configured = get_model_ui(spec.model._meta.label_lower).list.title_field
    if configured:
        return configured
    return next((name for name in ("title", "name", "code")
                 if name in spec.read_fields and not spec.model._meta.get_field(name).is_relation), None)


def safe_record_label(record, spec):
    name = _title_field(spec)
    value = getattr(record, name) if name else None
    return str(value) if isinstance(value, (str, int, float)) and value != "" else str(record.sysid)


def list_fields(spec):
    """Include every service metadata dependency to prevent deferred-load N+1."""
    columns = get_model_ui(spec.model._meta.label_lower).list.columns
    title = _title_field(spec)
    return tuple(dict.fromkeys((*SYSTEM_FIELDS, *[name for name in columns if name != "__label__"],
                                *([title] if title else []))))


def _field_label(spec, name):
    if name == "__label__":
        return "Registro"
    for section in get_model_ui(spec.model._meta.label_lower).form.sections:
        for field in section.fields:
            if field.name == name and field.label is not None:
                return field.label
    return str(spec.model._meta.get_field(name).verbose_name).capitalize()


def _value(field, value):
    if value is None:
        return UNAVAILABLE if field.is_relation else "—"
    if field.is_relation:
        return str(value)
    if field.choices:
        label = dict(field.flatchoices).get(value, value)
        return str(label)
    if isinstance(value, bool):
        return "Sim" if value else "Não"
    if isinstance(value, datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
        return formats.date_format(value, "DATETIME_FORMAT")
    if isinstance(value, date):
        return formats.date_format(value, "DATE_FORMAT")
    if isinstance(value, time):
        return formats.time_format(value, "TIME_FORMAT")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(formats.localize(value)) if value != "" else "—"


def list_context(spec, records, *, user, params=None):
    ui = get_model_ui(spec.model._meta.label_lower).list
    records = list(records)
    projected = records_data(records, list_fields(spec), user=user)
    rows = []
    for record, values in zip(records, projected):
        rows.append({"sysid": str(record.sysid), "label": safe_record_label(record, spec),
                     "cells": [safe_record_label(record, spec) if name == "__label__" else
                               _value(spec.model._meta.get_field(name), values.get(name))
                               for name in ui.columns]})
    params = params or {}
    return {"headings": [_field_label(spec, name) for name in ui.columns],
            "record_rows": rows,
            "filters": [{"name": name, "label": _field_label(spec, name),
                         "value": params.get(name, "")} for name in ui.filters]}


def grouped_specs(specs):
    """Group already-authorized projected catalogue entries without database work."""
    groups = {}
    for entry in specs:
        app_label = entry["table_key"].split(".", 1)[0]
        if app_label not in groups:
            try:
                label = get_app(app_label).label
            except KeyError:
                label = str(apps.get_app_config(app_label).verbose_name)
            groups[app_label] = {"app_label": app_label, "label": label, "models": []}
        groups[app_label]["models"].append(entry)
    return sorted(groups.values(), key=lambda group: (group["label"].casefold(), group["app_label"]))


def _layout(spec):
    layout = get_model_ui(spec.model._meta.label_lower).form
    sections = layout.sections or (FormSectionSpec("fields", "Dados do registro",
                                                  tuple(FieldUISpec(name) for name in spec.write_fields)),)
    return layout, sections


def _tabs(layout, sections):
    ids = {section["tab_id"] for section in sections}
    return [{"id": tab.id, "label": tab.label} for tab in layout.tabs if tab.id in ids]


def _picker_url(spec, name, instance, operation):
    organization_id = current_context().organization_id
    if organization_id is None:
        return None
    url = reverse("web:reference-picker", kwargs={"organization_id": organization_id,
                  "table_key": spec.model._meta.label_lower, "field_name": name})
    params = {"record": str(instance.pk)} if instance is not None else {}
    if operation == "access":
        params["access"] = "1"
    return url + "?" + urlencode(params)


def _accessible(bound):
    identifiers = []
    if bound.help_text:
        identifiers.append(bound.auto_id + "_helptext")
    if bound.errors:
        identifiers.append(bound.auto_id + "_error")
        bound.field.widget.attrs["aria-invalid"] = "true"
    if identifiers:
        bound.field.widget.attrs["aria-describedby"] = " ".join(identifiers)


def resolve_field_span(col_span, columns, *, wide=False):
    """Resolve the grid once, preserving source order and explicit dimensions."""
    if (col_span is not None and (type(col_span) is not int or not 1 <= col_span <= 12)
            or type(columns) is not int or columns not in (1, 2) or type(wide) is not bool):
        raise ValueError("Invalid CRUD field dimensions.")
    return col_span if col_span is not None else (12 if wide or columns == 1 else 6)


def form_sections(form, spec, *, instance=None, user, operation="edit"):
    if operation in ("delete", "restore"):
        return {"sections": [], "tabs": [], "active_tab": None}
    if operation == "access":
        fields = []
        for bound in form.visible_fields():
            _accessible(bound)
            entry = {"bound": bound, "label": bound.label, "help_text": bound.help_text,
                     "read_only": False, "col_span": resolve_field_span(None, 1)}
            if spec.model._meta.get_field(bound.name).is_relation:
                entry.update(picker_url=_picker_url(spec, bound.name, instance, operation),
                             table_key=spec.model._meta.label_lower,
                             record_id=str(instance.pk) if instance else "")
            fields.append(entry)
        return {"sections": [{"id": "access", "label": "Gerenciar acesso", "columns": 1,
                              "tab_id": None, "fields": fields}], "tabs": [], "active_tab": None}
    layout, declared_sections = _layout(spec)
    read_names = tuple(item.name for section in declared_sections for item in section.fields
                       if item.read_only and item.name in (set(spec.read_fields) | SYSTEM_READ_FIELDS))
    values = records_data([instance], read_names, user=user)[0] if instance is not None and read_names else {}
    sections = []
    active_tab = None
    for declared in declared_sections:
        fields = []
        for item in declared.fields:
            field = spec.model._meta.get_field(item.name)
            if item.read_only:
                if instance is not None and item.name in values:
                    fields.append({"label": item.label or _field_label(spec, item.name),
                                   "read_only": True, "value": _value(field, values[item.name]),
                                   "help_text": item.help_text if item.help_text is not None else field.help_text,
                                   "col_span": resolve_field_span(item.col_span, declared.columns,
                                       wide=field.get_internal_type() in ("TextField", "JSONField"))})
                continue
            if item.name not in form.fields:
                continue
            bound = form[item.name]
            _accessible(bound)
            entry = {"bound": bound, "label": bound.label, "help_text": bound.help_text,
                     "read_only": False, "col_span": resolve_field_span(item.col_span, declared.columns,
                         wide=isinstance(bound.field.widget, forms.Textarea))}
            control = CHANGE_PREFIX + item.name
            if control in form.fields:
                entry["change_bound"] = form[control]
                _accessible(entry["change_bound"])
            if field.is_relation:
                entry["picker_url"] = _picker_url(spec, item.name, instance, operation)
                entry["table_key"] = spec.model._meta.label_lower
                entry["record_id"] = str(instance.pk) if instance else ""
            fields.append(entry)
            if bound.errors and active_tab is None and declared.tab_id:
                active_tab = declared.tab_id
        if fields:
            sections.append({"id": declared.id, "label": declared.label, "columns": declared.columns,
                             "tab_id": declared.tab_id, "fields": fields})
    tabs = _tabs(layout, sections)
    return {"sections": sections, "tabs": tabs,
            "active_tab": active_tab or (tabs[0]["id"] if tabs else None)}


def detail_sections(record, spec, *, user):
    layout, declared_sections = _layout(spec)
    permitted = tuple(dict.fromkeys((*spec.read_fields, *SYSTEM_FIELDS)))
    values = records_data([record], permitted, user=user)[0]
    sections = []
    seen = set()
    for declared in declared_sections:
        fields = []
        for item in declared.fields:
            if item.name not in permitted:
                continue
            field = spec.model._meta.get_field(item.name)
            fields.append({"label": item.label or _field_label(spec, item.name),
                           "help_text": item.help_text if item.help_text is not None else field.help_text, "read_only": True,
                           "value": _value(field, values.get(item.name)),
                           "col_span": resolve_field_span(item.col_span, declared.columns,
                               wide=field.get_internal_type() in ("TextField", "JSONField"))})
            seen.add(item.name)
        if fields:
            sections.append({"id": declared.id, "label": declared.label, "columns": declared.columns,
                             "tab_id": declared.tab_id, "fields": fields})
    remaining = [name for name in permitted if name not in seen]
    if remaining:
        sections.append({"id": "system-details", "label": "Outros dados", "columns": 2, "tab_id": None,
                         "fields": [{"label": _field_label(spec, name), "read_only": True,
                                     "value": _value(spec.model._meta.get_field(name), values.get(name)),
                                     "col_span": resolve_field_span(None, 2,
                                         wide=spec.model._meta.get_field(name).get_internal_type() in ("TextField", "JSONField"))}
                                    for name in remaining]})
    tabs = _tabs(layout, sections)
    return {"sections": sections, "tabs": tabs, "active_tab": tabs[0]["id"] if tabs else None}
