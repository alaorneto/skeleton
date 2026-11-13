"""Manifest v2 presentation JSON: validation and conversion to specifications. Free of Django."""

import re

from .specs import (
    _IDENTIFIER,
    FIELD_DIMENSIONS_CORE_MINIMUM,
    MANIFEST_V2_CORE_MINIMUM,
    MAX_ADMIN_ENTRIES,
    MAX_HELP_TEXT_LENGTH,
    MAX_LABEL_LENGTH,
    MAX_LAYOUT_FIELDS,
    MAX_MODEL_UI,
    WIDGETS,
    AdministrationSpec,
    AdminScreenSpec,
    AdminSectionSpec,
    FieldUISpec,
    FormLayoutSpec,
    FormSectionSpec,
    ListSpec,
    ModelUISpec,
    TabSpec,
)

SCREEN_FIELDS = (
    "id",
    "section_id",
    "label",
    "scope",
    "kind",
    "order",
    "url_name",
    "handler_id",
    "organization_admin_only",
    "sensitive",
)


class _Checks:
    """The small checks every block shares, raising the caller's error_class."""

    def __init__(self, app_label, error_class):
        self.app_label = app_label
        self.error_class = error_class

    def fail(self, message):
        raise self.error_class(message)

    def obj(self, value, allowed, required=()):
        if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
            self.fail("Unknown or missing presentation fields.")

    def entries(self, value, maximum):
        if not isinstance(value, (list, tuple)) or len(value) > maximum:
            self.fail("Presentation entry limit exceeded or invalid list.")
        return value

    def text(self, value, maximum=MAX_LABEL_LENGTH, *, empty=False):
        if not isinstance(value, str) or (not value and not empty) or len(value) > maximum or "\x00" in value:
            self.fail("Invalid presentation text.")

    def identifier(self, value):
        if not isinstance(value, str) or len(value) > MAX_LABEL_LENGTH or not _IDENTIFIER.fullmatch(value):
            self.fail("Presentation IDs require simple identifiers.")

    def table(self, value):
        if not isinstance(value, str) or not re.fullmatch(re.escape(self.app_label) + r"\.[a-z][a-z0-9_]*", value):
            self.fail("Presentation model must belong to the application.")

    def unique(self, values, message):
        if len(values) != len(set(values)):
            self.fail(message)

    def sections(self, value):
        for item in self.entries(value, MAX_ADMIN_ENTRIES):
            self.obj(item, ("id", "label", "order"), ("id", "label"))
            self.identifier(item["id"])
            self.text(item["label"])
            if type(item.get("order", 0)) is not int:
                self.fail("Section order must be an integer.")
        ids = [item["id"] for item in value]
        self.unique(ids, "Duplicate presentation section ID.")
        return set(ids)


def _validate_tenant_screen(c, screen):
    if screen.get("handler_id") is not None:
        c.fail("Tenant screens cannot declare platform handlers.")
    if screen["kind"] == "custom":
        name = screen.get("url_name")
        if (
            not isinstance(name, str)
            or len(name) > MAX_LABEL_LENGTH
            or not re.fullmatch(re.escape(c.app_label) + r"(?:\:[a-zA-Z_][a-zA-Z0-9_]*)+", name)
        ):
            c.fail("Custom tenant URL must use the application namespace.")
        if not screen.get("organization_admin_only", False):
            c.fail("Tenant custom parameter screens require organization-admin restriction.")
    elif screen.get("url_name") is not None:
        c.fail("Only custom tenant screens may declare a URL.")


def _validate_screen(c, screen, section_ids):
    c.obj(screen, SCREEN_FIELDS, ("id", "section_id", "label", "scope", "kind"))
    c.identifier(screen["id"])
    c.identifier(screen["section_id"])
    c.text(screen["label"])
    if screen["section_id"] not in section_ids:
        c.fail("Administrative screen requires a declared section.")
    if screen["scope"] not in ("platform", "organization") or screen["kind"] not in ("configuration", "custom"):
        c.fail("Invalid administrative screen scope/kind.")
    if type(screen.get("order", 0)) is not int or any(
        type(screen.get(key, False)) is not bool for key in ("organization_admin_only", "sensitive")
    ):
        c.fail("Invalid administrative screen order/flags.")
    if screen["scope"] == "platform":
        if (
            screen["kind"] != "custom"
            or not screen.get("handler_id")
            or screen.get("url_name") is not None
            or screen.get("organization_admin_only", False)
        ):
            c.fail("Platform parameter screens require an owned handler, not tenant resources.")
        c.identifier(screen["handler_id"])
    else:
        _validate_tenant_screen(c, screen)


def _validate_administration(c, runtime):
    """Sections and screens; returns the configuration section IDs."""
    administration = runtime.get("administration", {})
    c.obj(administration, ("configuration_sections", "sections", "screens"))
    configuration_ids = c.sections(administration.get("configuration_sections", []))
    section_ids = c.sections(administration.get("sections", []))
    screens = c.entries(administration.get("screens", []), MAX_ADMIN_ENTRIES)
    for screen in screens:
        _validate_screen(c, screen, section_ids)
    c.unique([screen["id"] for screen in screens], "Duplicate administrative screen ID.")
    return configuration_ids


def _validate_configuration_fields(c, runtime, configuration_ids):
    # Precondition: each item is an object; the manifest validation (packages) checks it first.
    configuration = c.entries(runtime.get("configuration_fields", []), MAX_LAYOUT_FIELDS)
    for item in configuration:
        if item.get("label") is not None:
            c.text(item["label"])
        c.text(item.get("help_text", ""), MAX_HELP_TEXT_LENGTH, empty=True)
        if item.get("section_id") is not None:
            c.identifier(item["section_id"])
            if item["section_id"] not in configuration_ids:
                c.fail("Configuration field references an unknown section.")


def _validate_list(c, listing):
    c.obj(listing, ("columns", "filters", "title_field"))
    for key in ("columns", "filters"):
        if key in listing:
            names = c.entries(listing[key], MAX_LAYOUT_FIELDS)
            if key == "columns" and not names:
                c.fail("A model list requires at least one column.")
            for name in names:
                if key == "columns" and name == "__label__":
                    continue
                c.identifier(name)
            c.unique(names, "Duplicate list column/filter.")
    if listing.get("title_field") is not None:
        c.identifier(listing["title_field"])


def _validate_form_field(c, field, field_names):
    if isinstance(field, str):
        c.identifier(field)
        field_names.append(field)
        return
    c.obj(field, ("name", "label", "help_text", "widget", "read_only", "col_span", "rows"), ("name",))
    c.identifier(field["name"])
    field_names.append(field["name"])
    if field.get("label") is not None:
        c.text(field["label"])
    if field.get("help_text") is not None:
        c.text(field["help_text"], MAX_HELP_TEXT_LENGTH, empty=True)
    if field.get("widget") is not None and field["widget"] not in WIDGETS:
        c.fail("Unknown presentation widget.")
    if type(field.get("read_only", False)) is not bool:
        c.fail("read_only must be boolean.")
    for dimension, maximum in (("col_span", 12), ("rows", 40)):
        value = field.get(dimension)
        if value is not None and (type(value) is not int or not 1 <= value <= maximum):
            c.fail(f"{dimension} must be an integer from 1 to {maximum} or null.")
    if field.get("rows") is not None and (
        field.get("read_only", False) or field.get("widget") not in (None, "textarea", "json")
    ):
        c.fail("rows requires an editable textarea widget.")


def _validate_form_layout(c, layout):
    c.obj(layout, ("sections", "tabs"))
    tabs = c.entries(layout.get("tabs", []), MAX_ADMIN_ENTRIES)
    for tab in tabs:
        c.obj(tab, ("id", "label"), ("id", "label"))
        c.identifier(tab["id"])
        c.text(tab["label"])
    c.unique([tab["id"] for tab in tabs], "Duplicate form tab ID.")
    tab_ids = {tab["id"] for tab in tabs}
    form_sections = c.entries(layout.get("sections", []), MAX_ADMIN_ENTRIES)
    field_count = 0
    field_names = []
    for section in form_sections:
        c.obj(section, ("id", "label", "fields", "tab_id", "columns"), ("id", "label", "fields"))
        c.identifier(section["id"])
        c.text(section["label"])
        if section.get("tab_id") is not None and section["tab_id"] not in tab_ids:
            c.fail("Form section references an unknown tab.")
        if type(section.get("columns", 1)) is not int or section.get("columns", 1) not in (1, 2):
            c.fail("Form sections support one or two columns.")
        fields = c.entries(section["fields"], MAX_LAYOUT_FIELDS)
        field_count += len(fields)
        for field in fields:
            _validate_form_field(c, field, field_names)
    if field_count > MAX_LAYOUT_FIELDS:
        c.fail("Form field entry limit exceeded.")
    c.unique(field_names, "A form field may appear only once.")
    c.unique([section["id"] for section in form_sections], "Duplicate form section ID.")
    if tab_ids - {section.get("tab_id") for section in form_sections}:
        c.fail("Every form tab must contain a section.")


def _validate_model_ui(c, runtime):
    models = c.entries(runtime.get("model_ui", []), MAX_MODEL_UI)
    for model in models:
        c.obj(model, ("table_key", "list", "form"), ("table_key",))
        c.table(model["table_key"])
        _validate_list(c, model.get("list", {}))
        _validate_form_layout(c, model.get("form", {}))
    c.unique([model["table_key"] for model in models], "Duplicate model presentation.")


def validate_runtime_presentation(runtime, app_label, *, error_class=ValueError):
    """Validate v2 JSON before loading any Django or publisher code; never normalize it.

    The blocks run in their original order: the first failure decides the message.
    """
    c = _Checks(app_label, error_class)
    configuration_ids = _validate_administration(c, runtime)
    _validate_configuration_fields(c, runtime, configuration_ids)
    _validate_model_ui(c, runtime)


def required_presentation_core(runtime) -> str:
    """Return the minimum core for validated JSON without adding signed defaults."""
    for model in runtime.get("model_ui", ()):
        for section in model.get("form", {}).get("sections", ()):
            for field in section["fields"]:
                if isinstance(field, dict) and ("col_span" in field or "rows" in field):
                    return FIELD_DIMENSIONS_CORE_MINIMUM
    return MANIFEST_V2_CORE_MINIMUM


def presentation_from_runtime(runtime):
    administration = runtime.get("administration", {})
    admin = AdministrationSpec(
        configuration_sections=tuple(
            AdminSectionSpec(**item) for item in administration.get("configuration_sections", ())
        ),
        sections=tuple(AdminSectionSpec(**item) for item in administration.get("sections", ())),
        screens=tuple(AdminScreenSpec(**item) for item in administration.get("screens", ())),
    )
    ui = []
    for item in runtime.get("model_ui", ()):
        listing = item.get("list", {})
        layout = item.get("form", {})
        ui.append(
            ModelUISpec(
                item["table_key"],
                ListSpec(
                    tuple(listing.get("columns", ListSpec().columns)),
                    tuple(listing.get("filters", ())),
                    listing.get("title_field"),
                ),
                FormLayoutSpec(
                    tuple(
                        FormSectionSpec(
                            section["id"],
                            section["label"],
                            tuple(
                                FieldUISpec(field) if isinstance(field, str) else FieldUISpec(**field)
                                for field in section["fields"]
                            ),
                            section.get("tab_id"),
                            section.get("columns", 1),
                        )
                        for section in layout.get("sections", ())
                    ),
                    tuple(TabSpec(**tab) for tab in layout.get("tabs", ())),
                ),
            )
        )
    return admin, tuple(ui)
