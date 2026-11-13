"""Presentation checks against Django models, routes and handlers. Only metadata, never records."""

from dataclasses import asdict

from .routes import _validate_owned_routes, _validate_reserved_routes
from .runtime import validate_runtime_presentation
from .specs import SYSTEM_READ_FIELDS, ModelUISpec


def _widget_allowed(field, widget):
    from django.db import models

    if isinstance(field, models.FileField):
        return False
    if widget is None:
        return isinstance(
            field,
            (
                models.CharField,
                models.TextField,
                models.IntegerField,
                models.FloatField,
                models.DecimalField,
                models.BooleanField,
                models.DateField,
                models.TimeField,
                models.UUIDField,
                models.JSONField,
                models.ForeignKey,
            ),
        )
    if field.is_relation:
        return widget == "reference"
    if widget == "select":
        return bool(field.choices)
    if widget == "text":
        return isinstance(field, (models.CharField, models.TextField))
    if widget == "textarea":
        return isinstance(field, (models.CharField, models.TextField))
    if widget == "number":
        return isinstance(field, (models.IntegerField, models.FloatField, models.DecimalField))
    if widget == "checkbox":
        return isinstance(field, models.BooleanField) and not field.null
    if widget == "datetime":
        return isinstance(field, models.DateTimeField)
    if widget == "date":
        return isinstance(field, models.DateField) and not isinstance(field, models.DateTimeField)
    if widget == "time":
        return isinstance(field, models.TimeField)
    if widget == "uuid":
        return isinstance(field, models.UUIDField)
    if widget == "json":
        return isinstance(field, models.JSONField)
    return False


def _owned_spec(manifest, key):
    from django.core.exceptions import ValidationError

    from skeleton.common.models import SecuredRecord

    from ..registry import get_spec

    try:
        spec = get_spec(key)
    except KeyError as exc:
        raise ValidationError(f"{key}: presentation references an unknown model.") from exc
    if (
        spec.model._meta.app_label != manifest.app_label
        or spec.scope != "organization"
        or not spec.expose_api
        or not issubclass(spec.model, SecuredRecord)
    ):
        raise ValidationError(f"{key}: presentation requires an owned exposed SecuredRecord.")
    return spec


def _validate_list_presentation(key, spec, ui, concrete, readable):
    from django.core.exceptions import ValidationError
    from django.db import models

    for name in (*ui.list.columns, *ui.list.filters, *((ui.list.title_field,) if ui.list.title_field else ())):
        if name == "__label__" and name in ui.list.columns:
            continue
        if name not in concrete or name not in readable:
            raise ValidationError(f"{key}.{name}: list fields must be concrete and readable.")
    if ui.list.title_field:
        field = concrete[ui.list.title_field]
        if (
            field.is_relation
            or isinstance(field, (models.JSONField, models.FileField))
            or not _widget_allowed(field, None)
        ):
            raise ValidationError(f"{key}.{ui.list.title_field}: title field must be a readable scalar.")
    for name in ui.list.filters:
        field = concrete[name]
        if (
            name not in set(spec.read_fields) | {"sysid", "owner", "visibility"}
            or isinstance(field, models.JSONField)
            or (field.is_relation and name != "owner")
        ):
            raise ValidationError(f"{key}.{name}: unsupported exact filter.")


def _validate_form_entry(key, spec, entry, concrete, readable, editable):
    from django import forms
    from django.core.exceptions import ValidationError

    if entry.name not in concrete:
        raise ValidationError(f"{key}.{entry.name}: unknown form field.")
    if entry.read_only:
        if entry.name not in readable:
            raise ValidationError(f"{key}.{entry.name}: read-only form field is not readable.")
    else:
        if entry.name not in spec.write_fields:
            raise ValidationError(f"{key}.{entry.name}: form field is not writable.")
        editable.append(entry.name)
    if not _widget_allowed(concrete[entry.name], entry.widget):
        raise ValidationError(f"{key}.{entry.name}: incompatible or unsupported widget.")
    if entry.rows is not None:
        field = concrete[entry.name]
        if entry.read_only or field.is_relation:
            raise ValidationError("rows requires an editable textarea widget.")
        if entry.widget is None:
            formfield = field.formfield()
            if formfield is None or not isinstance(formfield.widget, forms.Textarea):
                raise ValidationError("rows requires an editable textarea widget.")


def _validate_form_presentation(key, spec, ui, concrete, readable):
    from django.core.exceptions import ValidationError

    editable = []
    for section in ui.form.sections:
        for entry in section.fields:
            _validate_form_entry(key, spec, entry, concrete, readable, editable)
    if ui.form.sections and (len(editable) != len(set(editable)) or set(editable) != set(spec.write_fields)):
        raise ValidationError(f"{key}: form must cover every writable field exactly once.")
    for name in spec.write_fields:
        if name not in concrete or not _widget_allowed(concrete[name], None):
            raise ValidationError(f"{key}.{name}: unsupported generic form field.")


def _validate_model_presentation(manifest):
    """Every exposed table of the app against its list and form presentation."""
    from django.core.exceptions import ValidationError

    from ..registry import all_specs

    declared = {item.table_key: item for item in manifest.model_ui}
    for key in declared:
        _owned_spec(manifest, key)
    for spec in all_specs():
        key = spec.model._meta.label_lower
        if spec.model._meta.app_label != manifest.app_label or not spec.expose_api:
            continue
        if len(spec.write_fields) != len(set(spec.write_fields)):
            raise ValidationError(f"{key}: duplicate writable fields.")
        _owned_spec(manifest, key)
        ui = declared.get(key, ModelUISpec(key))
        concrete = {field.name: field for field in spec.model._meta.concrete_fields}
        readable = set(spec.read_fields) | SYSTEM_READ_FIELDS
        _validate_list_presentation(key, spec, ui, concrete, readable)
        _validate_form_presentation(key, spec, ui, concrete, readable)


def _validate_application_routes(manifest):
    """Own namespaces, reserved roots and owned code; returns the tenant routes, if any."""
    from django.apps import apps
    from django.core.exceptions import ValidationError
    from django.urls import include, path

    routes = None
    if manifest.urls:
        module, app_name, namespace = include(manifest.urls)
        if app_name != manifest.app_label or namespace not in (None, manifest.app_label):
            raise ValidationError("Application URLs require their own app-label namespace.")
        patterns = module.urlpatterns if hasattr(module, "urlpatterns") else module
        _validate_reserved_routes(patterns)
        _validate_owned_routes(patterns, apps.get_app_config(manifest.app_label).name)
        routes = (
            path(
                "o/<uuid:organization_id>/apps/" + manifest.app_label + "/",
                include((patterns, app_name), namespace=manifest.app_label),
            ),
        )
    if manifest.api_urls:
        module, app_name, namespace = include(manifest.api_urls)
        own_names = (manifest.app_label, manifest.app_label + "_api")
        if app_name not in own_names or namespace not in (None, app_name):
            raise ValidationError("Application API URLs require their own app-label namespace.")
        patterns = module.urlpatterns if hasattr(module, "urlpatterns") else module
        _validate_owned_routes(patterns, apps.get_app_config(manifest.app_label).name)
    return routes


def _validate_navigation(manifest, routes):
    from uuid import UUID

    from django.core.exceptions import ValidationError
    from django.urls import NoReverseMatch, reverse

    for entry in manifest.navigation:
        _owned_spec(manifest, entry.table_key)
        try:
            if routes is None or not entry.url_name.startswith(manifest.app_label + ":"):
                raise NoReverseMatch()
            reverse(entry.url_name, urlconf=routes, kwargs={"organization_id": UUID(int=1)})
        except NoReverseMatch as exc:
            raise ValidationError("Application navigation must resolve within its own namespace.") from exc


def _validate_screens(manifest, routes):
    from uuid import UUID

    from django.core.exceptions import ValidationError
    from django.urls import NoReverseMatch, reverse

    from . import get_admin_handler  # The package registry: tests and apps patch it there.

    for screen in manifest.administration.screens:
        if screen.handler_id:
            try:
                get_admin_handler(manifest.app_label, screen.handler_id)
            except KeyError as exc:
                raise ValidationError(f"{screen.id}: administrative handler is not registered.") from exc
        if screen.url_name:
            try:
                if routes is None:
                    raise NoReverseMatch()
                reverse(screen.url_name, urlconf=routes, kwargs={"organization_id": UUID(int=1)})
            except NoReverseMatch as exc:
                raise ValidationError(f"{screen.id}: custom URL cannot resolve within its application.") from exc


def validate_presentation(manifest) -> None:
    """Django model/route/handler checks. Only metadata is inspected, never records."""
    from django.core.exceptions import ValidationError

    validate_runtime_presentation(
        {
            "administration": asdict(manifest.administration),
            "model_ui": [asdict(item) for item in manifest.model_ui],
            "configuration_fields": [asdict(item) for item in manifest.configuration_fields],
        },
        manifest.app_label,
        error_class=ValidationError,
    )
    _validate_model_presentation(manifest)
    routes = _validate_application_routes(manifest)
    _validate_navigation(manifest, routes)
    _validate_screens(manifest, routes)
