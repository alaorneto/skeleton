"""Closed presentation metadata of apps and the process-local registries (#332).

``specs`` and ``runtime`` are free of Django, so the packager validates manifests with them;
``routes`` and ``model_validation`` import Django inside their functions. Nothing in the package
touches the database. The registries live here, in the package namespace, because tests and
callers patch ``skeleton.system.presentation._handlers`` and ``get_model_ui`` by this path.
"""

from dataclasses import asdict

from .model_validation import _widget_allowed as _widget_allowed
from .model_validation import validate_presentation
from .routes import RESERVED_ROOTS
from .routes import _validate_owned_routes as _validate_owned_routes
from .routes import _validate_reserved_routes as _validate_reserved_routes
from .runtime import presentation_from_runtime, required_presentation_core, validate_runtime_presentation
from .specs import (
    _IDENTIFIER,
    FIELD_DIMENSIONS_CORE_MINIMUM,
    MANIFEST_V2_CORE_MINIMUM,
    MAX_ADMIN_ENTRIES,
    MAX_HELP_TEXT_LENGTH,
    MAX_LABEL_LENGTH,
    MAX_LAYOUT_FIELDS,
    MAX_MODEL_UI,
    SYSTEM_READ_FIELDS,
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

__all__ = [
    "FIELD_DIMENSIONS_CORE_MINIMUM",
    "MANIFEST_V2_CORE_MINIMUM",
    "MAX_ADMIN_ENTRIES",
    "MAX_HELP_TEXT_LENGTH",
    "MAX_LABEL_LENGTH",
    "MAX_LAYOUT_FIELDS",
    "MAX_MODEL_UI",
    "RESERVED_ROOTS",
    "SYSTEM_READ_FIELDS",
    "WIDGETS",
    "AdminScreenSpec",
    "AdminSectionSpec",
    "AdministrationSpec",
    "FieldUISpec",
    "FormLayoutSpec",
    "FormSectionSpec",
    "ListSpec",
    "ModelUISpec",
    "TabSpec",
    "get_admin_handler",
    "get_model_ui",
    "presentation_from_runtime",
    "register_admin_handler",
    "register_presentation",
    "required_presentation_core",
    "unregister_presentation",
    "validate_presentation",
    "validate_runtime_presentation",
]

_model_ui = {}
_handlers = {}


def register_presentation(manifest):
    from django.core.exceptions import ValidationError

    runtime = {
        "administration": asdict(manifest.administration),
        "model_ui": [asdict(item) for item in manifest.model_ui],
        "configuration_fields": [asdict(item) for item in manifest.configuration_fields],
    }
    validate_runtime_presentation(runtime, manifest.app_label, error_class=ValidationError)
    for item in manifest.model_ui:
        if item.table_key in _model_ui and _model_ui[item.table_key] != item:
            raise ValidationError("Duplicate model presentation registration.")
    _model_ui.update((item.table_key, item) for item in manifest.model_ui)


def unregister_presentation(app_label):
    for key in tuple(_model_ui):
        if key.startswith(app_label + "."):
            del _model_ui[key]
    for key in tuple(_handlers):
        if key[0] == app_label:
            del _handlers[key]
    from ..record_operations import unregister_record_operations

    unregister_record_operations(app_label)


def get_model_ui(table_key: str) -> ModelUISpec:
    try:
        return _model_ui[table_key]
    except KeyError:
        return ModelUISpec(table_key)


def register_admin_handler(app_label, handler_id, handler):
    from django.apps import apps
    from django.core.exceptions import ValidationError

    from ..app_registry import get_app

    try:
        get_app(app_label)
        config = apps.get_app_config(app_label)
    except (KeyError, LookupError) as exc:
        raise ValidationError("Administrative handlers require a registered owning application.") from exc
    module = getattr(handler, "__module__", "")
    if (
        not isinstance(handler_id, str)
        or len(handler_id) > MAX_LABEL_LENGTH
        or not _IDENTIFIER.fullmatch(handler_id)
        or not callable(handler)
        or not (module == config.name or module.startswith(config.name + "."))
    ):
        raise ValidationError("Administrative handler must be callable code owned by its application.")
    from inspect import signature

    try:
        signature(handler).bind(None)
    except (ValueError, TypeError) as exc:
        raise ValidationError("Administrative handler must accept a request-only invocation.") from exc
    key = (app_label, handler_id)
    if key in _handlers and _handlers[key] is not handler:
        raise ValidationError("Duplicate administrative handler ID.")
    _handlers[key] = handler
    return handler


def get_admin_handler(app_label, handler_id):
    return _handlers[(app_label, handler_id)]
