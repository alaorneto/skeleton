"""skeleton.system.presentation is a package that keeps every importable name (#332)."""

import subprocess
import sys

from skeleton.system import presentation

NAMES = (
    "AdministrationSpec",
    "AdminScreenSpec",
    "AdminSectionSpec",
    "FieldUISpec",
    "FormLayoutSpec",
    "FormSectionSpec",
    "ListSpec",
    "ModelUISpec",
    "TabSpec",
    "presentation_from_runtime",
    "register_presentation",
    "unregister_presentation",
    "validate_presentation",
    "validate_runtime_presentation",
    "required_presentation_core",
    "get_model_ui",
    "get_admin_handler",
    "register_admin_handler",
    "FIELD_DIMENSIONS_CORE_MINIMUM",
    "MANIFEST_V2_CORE_MINIMUM",
    "SYSTEM_READ_FIELDS",
    "MAX_ADMIN_ENTRIES",
    "MAX_MODEL_UI",
    "MAX_LAYOUT_FIELDS",
    "MAX_LABEL_LENGTH",
    "MAX_HELP_TEXT_LENGTH",
    "WIDGETS",
    "RESERVED_ROOTS",
    "_IDENTIFIER",
    "_widget_allowed",
    "_validate_reserved_routes",
    "_validate_owned_routes",
    "_model_ui",
    "_handlers",
)


def test_public_names_stay_importable():
    assert [name for name in NAMES if not hasattr(presentation, name)] == []
    assert presentation.__file__.endswith("__init__.py")


def test_registries_live_in_package_namespace(monkeypatch):
    assert presentation.register_admin_handler.__module__ == "skeleton.system.presentation"
    assert presentation.get_model_ui.__module__ == "skeleton.system.presentation"
    marker = object()
    monkeypatch.setattr(presentation, "_handlers", {("app", "handler"): marker})
    assert presentation.get_admin_handler("app", "handler") is marker


def test_pure_modules_do_not_import_django():
    code = (
        "import sys, skeleton.system.presentation.specs, skeleton.system.presentation.runtime; "
        "assert 'django' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
