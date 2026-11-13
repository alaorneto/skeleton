"""Limits, widgets and the frozen specifications of presentation metadata. Free of Django."""

import re
from dataclasses import dataclass

from skeleton.common.field_names import SYSTEM_FIELD_NAMES

MAX_ADMIN_ENTRIES = 100
MAX_MODEL_UI = 200
MAX_LAYOUT_FIELDS = 200
MAX_LABEL_LENGTH = 200
MAX_HELP_TEXT_LENGTH = 2000
MANIFEST_V2_CORE_MINIMUM = "0.2.0"
FIELD_DIMENSIONS_CORE_MINIMUM = "0.3.0"
WIDGETS = frozenset(
    {"text", "textarea", "number", "checkbox", "select", "date", "datetime", "time", "uuid", "json", "reference"}
)
SYSTEM_READ_FIELDS = frozenset(SYSTEM_FIELD_NAMES)  # Single source in skeleton.common.field_names (#310).
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")


@dataclass(frozen=True)
class AdminSectionSpec:
    id: str
    label: str
    order: int = 0


@dataclass(frozen=True)
class AdminScreenSpec:
    """A configuration/parameter screen, never a business-table viewer or form."""

    id: str
    section_id: str
    label: str
    scope: str
    kind: str
    order: int = 0
    url_name: str | None = None
    handler_id: str | None = None
    organization_admin_only: bool = False
    sensitive: bool = False


@dataclass(frozen=True)
class AdministrationSpec:
    configuration_sections: tuple[AdminSectionSpec, ...] = ()
    sections: tuple[AdminSectionSpec, ...] = ()
    screens: tuple[AdminScreenSpec, ...] = ()


@dataclass(frozen=True)
class FieldUISpec:
    name: str
    label: str | None = None
    help_text: str | None = None
    widget: str | None = None
    read_only: bool = False
    col_span: int | None = None
    rows: int | None = None


@dataclass(frozen=True)
class FormSectionSpec:
    id: str
    label: str
    fields: tuple[FieldUISpec, ...]
    tab_id: str | None = None
    columns: int = 1


@dataclass(frozen=True)
class TabSpec:
    id: str
    label: str


@dataclass(frozen=True)
class FormLayoutSpec:
    sections: tuple[FormSectionSpec, ...] = ()
    tabs: tuple[TabSpec, ...] = ()


@dataclass(frozen=True)
class ListSpec:
    columns: tuple[str, ...] = ("__label__", "sysid", "updated_at")
    filters: tuple[str, ...] = ()
    title_field: str | None = None


@dataclass(frozen=True)
class ModelUISpec:
    table_key: str
    list: ListSpec = ListSpec()
    form: FormLayoutSpec = FormLayoutSpec()
