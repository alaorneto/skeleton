"""Closed presentation metadata and process-local registries, with no database access."""
from dataclasses import asdict, dataclass
import re


MAX_ADMIN_ENTRIES = 100
MAX_MODEL_UI = 200
MAX_LAYOUT_FIELDS = 200
MAX_LABEL_LENGTH = 200
MAX_HELP_TEXT_LENGTH = 2000
MANIFEST_V2_CORE_MINIMUM = '0.2.0'
FIELD_DIMENSIONS_CORE_MINIMUM = '0.3.0'
WIDGETS = frozenset({'text', 'textarea', 'number', 'checkbox', 'select', 'date', 'datetime', 'time', 'uuid', 'json', 'reference'})
SYSTEM_READ_FIELDS = frozenset({'sysid', 'created_at', 'updated_at', 'created_by', 'updated_by', 'deleted_at', 'deleted_by', 'revision'})
_IDENTIFIER = re.compile(r'[a-z][a-z0-9_]*\Z')


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
    columns: tuple[str, ...] = ('__label__', 'sysid', 'updated_at')
    filters: tuple[str, ...] = ()
    title_field: str | None = None


@dataclass(frozen=True)
class ModelUISpec:
    table_key: str
    list: ListSpec = ListSpec()
    form: FormLayoutSpec = FormLayoutSpec()


def validate_runtime_presentation(runtime, app_label, *, error_class=ValueError):
    """Validate v2 JSON before loading any Django or publisher code; never normalize it."""
    def fail(message):
        raise error_class(message)

    def obj(value, allowed, required=()):
        if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
            fail('Unknown or missing presentation fields.')

    def entries(value, maximum):
        if not isinstance(value, (list, tuple)) or len(value) > maximum:
            fail('Presentation entry limit exceeded or invalid list.')
        return value

    def text(value, maximum=MAX_LABEL_LENGTH, *, empty=False):
        if not isinstance(value, str) or (not value and not empty) or len(value) > maximum or '\x00' in value:
            fail('Invalid presentation text.')

    def identifier(value):
        if not isinstance(value, str) or len(value) > MAX_LABEL_LENGTH or not _IDENTIFIER.fullmatch(value):
            fail('Presentation IDs require simple identifiers.')

    def table(value):
        if not isinstance(value, str) or not re.fullmatch(re.escape(app_label) + r'\.[a-z][a-z0-9_]*', value):
            fail('Presentation model must belong to the application.')

    def unique(values, message):
        if len(values) != len(set(values)):
            fail(message)

    def sections(value):
        for item in entries(value, MAX_ADMIN_ENTRIES):
            obj(item, ('id', 'label', 'order'), ('id', 'label'))
            identifier(item['id'])
            text(item['label'])
            if type(item.get('order', 0)) is not int:
                fail('Section order must be an integer.')
        ids = [item['id'] for item in value]
        unique(ids, 'Duplicate presentation section ID.')
        return set(ids)

    administration = runtime.get('administration', {})
    obj(administration, ('configuration_sections', 'sections', 'screens'))
    configuration_ids = sections(administration.get('configuration_sections', []))
    section_ids = sections(administration.get('sections', []))
    screens = entries(administration.get('screens', []), MAX_ADMIN_ENTRIES)
    for screen in screens:
        obj(screen, ('id', 'section_id', 'label', 'scope', 'kind', 'order', 'url_name', 'handler_id', 'organization_admin_only', 'sensitive'), ('id', 'section_id', 'label', 'scope', 'kind'))
        identifier(screen['id'])
        identifier(screen['section_id'])
        text(screen['label'])
        if screen['section_id'] not in section_ids:
            fail('Administrative screen requires a declared section.')
        if screen['scope'] not in ('platform', 'organization') or screen['kind'] not in ('configuration', 'custom'):
            fail('Invalid administrative screen scope/kind.')
        if type(screen.get('order', 0)) is not int or any(type(screen.get(key, False)) is not bool for key in ('organization_admin_only', 'sensitive')):
            fail('Invalid administrative screen order/flags.')
        if screen['scope'] == 'platform':
            if screen['kind'] != 'custom' or not screen.get('handler_id') or screen.get('url_name') is not None or screen.get('organization_admin_only', False):
                fail('Platform parameter screens require an owned handler, not tenant resources.')
            identifier(screen['handler_id'])
        else:
            if screen.get('handler_id') is not None:
                fail('Tenant screens cannot declare platform handlers.')
            if screen['kind'] == 'custom':
                name = screen.get('url_name')
                if not isinstance(name, str) or len(name) > MAX_LABEL_LENGTH or not re.fullmatch(re.escape(app_label) + r'(?:\:[a-zA-Z_][a-zA-Z0-9_]*)+', name):
                    fail('Custom tenant URL must use the application namespace.')
                if not screen.get('organization_admin_only', False):
                    fail('Tenant custom parameter screens require organization-admin restriction.')
            elif screen.get('url_name') is not None:
                fail('Only custom tenant screens may declare a URL.')
    unique([screen['id'] for screen in screens], 'Duplicate administrative screen ID.')
    configuration = entries(runtime.get('configuration_fields', []), MAX_LAYOUT_FIELDS)
    for item in configuration:
        if item.get('label') is not None:
            text(item['label'])
        text(item.get('help_text', ''), MAX_HELP_TEXT_LENGTH, empty=True)
        if item.get('section_id') is not None:
            identifier(item['section_id'])
            if item['section_id'] not in configuration_ids:
                fail('Configuration field references an unknown section.')
    models = entries(runtime.get('model_ui', []), MAX_MODEL_UI)
    for model in models:
        obj(model, ('table_key', 'list', 'form'), ('table_key',))
        table(model['table_key'])
        listing = model.get('list', {})
        obj(listing, ('columns', 'filters', 'title_field'))
        for key in ('columns', 'filters'):
            if key in listing:
                names = entries(listing[key], MAX_LAYOUT_FIELDS)
                if key == 'columns' and not names:
                    fail('A model list requires at least one column.')
                for name in names:
                    if key == 'columns' and name == '__label__':
                        continue
                    identifier(name)
                unique(names, 'Duplicate list column/filter.')
        if listing.get('title_field') is not None:
            identifier(listing['title_field'])
        layout = model.get('form', {})
        obj(layout, ('sections', 'tabs'))
        tabs = entries(layout.get('tabs', []), MAX_ADMIN_ENTRIES)
        for tab in tabs:
            obj(tab, ('id', 'label'), ('id', 'label'))
            identifier(tab['id'])
            text(tab['label'])
        unique([tab['id'] for tab in tabs], 'Duplicate form tab ID.')
        tab_ids = {tab['id'] for tab in tabs}
        form_sections = entries(layout.get('sections', []), MAX_ADMIN_ENTRIES)
        field_count = 0
        field_names = []
        for section in form_sections:
            obj(section, ('id', 'label', 'fields', 'tab_id', 'columns'), ('id', 'label', 'fields'))
            identifier(section['id'])
            text(section['label'])
            if section.get('tab_id') is not None and section['tab_id'] not in tab_ids:
                fail('Form section references an unknown tab.')
            if type(section.get('columns', 1)) is not int or section.get('columns', 1) not in (1, 2):
                fail('Form sections support one or two columns.')
            fields = entries(section['fields'], MAX_LAYOUT_FIELDS)
            field_count += len(fields)
            for field in fields:
                if isinstance(field, str):
                    identifier(field)
                    field_names.append(field)
                    continue
                obj(field, ('name', 'label', 'help_text', 'widget', 'read_only', 'col_span', 'rows'), ('name',))
                identifier(field['name'])
                field_names.append(field['name'])
                if field.get('label') is not None:
                    text(field['label'])
                if field.get('help_text') is not None:
                    text(field['help_text'], MAX_HELP_TEXT_LENGTH, empty=True)
                if field.get('widget') is not None and field['widget'] not in WIDGETS:
                    fail('Unknown presentation widget.')
                if type(field.get('read_only', False)) is not bool:
                    fail('read_only must be boolean.')
                for dimension, maximum in (('col_span', 12), ('rows', 40)):
                    value = field.get(dimension)
                    if value is not None and (type(value) is not int or not 1 <= value <= maximum):
                        fail(f'{dimension} must be an integer from 1 to {maximum} or null.')
                if field.get('rows') is not None and (
                    field.get('read_only', False) or field.get('widget') not in (None, 'textarea', 'json')
                ):
                    fail('rows requires an editable textarea widget.')
        if field_count > MAX_LAYOUT_FIELDS:
            fail('Form field entry limit exceeded.')
        unique(field_names, 'A form field may appear only once.')
        unique([section['id'] for section in form_sections], 'Duplicate form section ID.')
        if tab_ids - {section.get('tab_id') for section in form_sections}:
            fail('Every form tab must contain a section.')
    unique([model['table_key'] for model in models], 'Duplicate model presentation.')


def required_presentation_core(runtime) -> str:
    """Return the minimum core for validated JSON without adding signed defaults."""
    for model in runtime.get('model_ui', ()):
        for section in model.get('form', {}).get('sections', ()):
            for field in section['fields']:
                if isinstance(field, dict) and ('col_span' in field or 'rows' in field):
                    return FIELD_DIMENSIONS_CORE_MINIMUM
    return MANIFEST_V2_CORE_MINIMUM


def presentation_from_runtime(runtime):
    administration = runtime.get('administration', {})
    admin = AdministrationSpec(
        configuration_sections=tuple(AdminSectionSpec(**item) for item in administration.get('configuration_sections', ())),
        sections=tuple(AdminSectionSpec(**item) for item in administration.get('sections', ())),
        screens=tuple(AdminScreenSpec(**item) for item in administration.get('screens', ())),
    )
    ui = []
    for item in runtime.get('model_ui', ()):
        listing = item.get('list', {})
        layout = item.get('form', {})
        ui.append(ModelUISpec(item['table_key'],
            ListSpec(tuple(listing.get('columns', ListSpec().columns)), tuple(listing.get('filters', ())), listing.get('title_field')),
            FormLayoutSpec(tuple(FormSectionSpec(
                section['id'], section['label'], tuple(FieldUISpec(field) if isinstance(field, str) else FieldUISpec(**field) for field in section['fields']),
                section.get('tab_id'), section.get('columns', 1)) for section in layout.get('sections', ())),
                tuple(TabSpec(**tab) for tab in layout.get('tabs', ())))))
    return admin, tuple(ui)


_model_ui = {}
_handlers = {}


def register_presentation(manifest):
    from django.core.exceptions import ValidationError
    runtime = {'administration': asdict(manifest.administration), 'model_ui': [asdict(item) for item in manifest.model_ui],
               'configuration_fields': [asdict(item) for item in manifest.configuration_fields]}
    validate_runtime_presentation(runtime, manifest.app_label, error_class=ValidationError)
    for item in manifest.model_ui:
        if item.table_key in _model_ui and _model_ui[item.table_key] != item:
            raise ValidationError('Duplicate model presentation registration.')
    _model_ui.update((item.table_key, item) for item in manifest.model_ui)


def unregister_presentation(app_label):
    for key in tuple(_model_ui):
        if key.startswith(app_label + '.'):
            del _model_ui[key]
    for key in tuple(_handlers):
        if key[0] == app_label:
            del _handlers[key]
    from .record_operations import unregister_record_operations
    unregister_record_operations(app_label)


def get_model_ui(table_key: str) -> ModelUISpec:
    try:
        return _model_ui[table_key]
    except KeyError:
        return ModelUISpec(table_key)


def register_admin_handler(app_label, handler_id, handler):
    from django.apps import apps
    from django.core.exceptions import ValidationError
    from .app_registry import get_app
    try:
        get_app(app_label)
        config = apps.get_app_config(app_label)
    except (KeyError, LookupError) as exc:
        raise ValidationError('Administrative handlers require a registered owning application.') from exc
    module = getattr(handler, '__module__', '')
    if not isinstance(handler_id, str) or len(handler_id) > MAX_LABEL_LENGTH or not _IDENTIFIER.fullmatch(handler_id) or not callable(handler) or not (module == config.name or module.startswith(config.name + '.')):
        raise ValidationError('Administrative handler must be callable code owned by its application.')
    from inspect import signature
    try:
        signature(handler).bind(None)
    except (ValueError, TypeError) as exc:
        raise ValidationError('Administrative handler must accept a request-only invocation.') from exc
    key = (app_label, handler_id)
    if key in _handlers and _handlers[key] is not handler:
        raise ValidationError('Duplicate administrative handler ID.')
    _handlers[key] = handler
    return handler


def get_admin_handler(app_label, handler_id):
    return _handlers[(app_label, handler_id)]


def _widget_allowed(field, widget):
    from django.db import models
    if isinstance(field, models.FileField):
        return False
    if widget is None:
        return isinstance(field, (models.CharField, models.TextField, models.IntegerField,
                                 models.FloatField, models.DecimalField, models.BooleanField,
                                 models.DateField, models.TimeField, models.UUIDField,
                                 models.JSONField, models.ForeignKey))
    if field.is_relation:
        return widget == 'reference'
    if widget == 'select':
        return bool(field.choices)
    if widget == 'text':
        return isinstance(field, (models.CharField, models.TextField))
    if widget == 'textarea':
        return isinstance(field, (models.CharField, models.TextField))
    if widget == 'number':
        return isinstance(field, (models.IntegerField, models.FloatField, models.DecimalField))
    if widget == 'checkbox':
        return isinstance(field, models.BooleanField) and not field.null
    if widget == 'datetime':
        return isinstance(field, models.DateTimeField)
    if widget == 'date':
        return isinstance(field, models.DateField) and not isinstance(field, models.DateTimeField)
    if widget == 'time':
        return isinstance(field, models.TimeField)
    if widget == 'uuid':
        return isinstance(field, models.UUIDField)
    if widget == 'json':
        return isinstance(field, models.JSONField)
    return False


def _validate_reserved_routes(patterns, prefix=''):
    from django.core.exceptions import ValidationError
    from django.urls import URLResolver
    from django.urls.resolvers import RoutePattern
    for pattern in patterns:
        if isinstance(pattern.pattern, RoutePattern):
            route = prefix + pattern.pattern._route
            first = route.split('/', 1)[0]
            if first == 'admin':
                raise ValidationError('Application URLs collide with reserved admin/ prefix.')
            if '<' in first:
                # Test just the first segment, independently of later path parameters.
                from django.urls.resolvers import RoutePattern as SegmentPattern
                if SegmentPattern(first + '/').regex.match('admin/') is not None:
                    raise ValidationError('Application URLs capture the reserved admin/ prefix.')
            if not first and isinstance(pattern, URLResolver):
                _validate_reserved_routes(pattern.url_patterns, route)
        else:
            expression = pattern.pattern.regex.pattern
            if expression in ('^$', '$', '') and not isinstance(pattern, URLResolver):
                continue
            if expression in ('^', '') and isinstance(pattern, URLResolver):
                _validate_reserved_routes(pattern.url_patterns, prefix)
                continue
            # Regex routes must prove a disjoint leading literal; catchalls/branches
            # cannot safely share the root with a core-owned administration prefix.
            literal = re.match(r'\^?([a-zA-Z0-9_/-]*)', expression).group(1)
            suffix = expression[len(literal) + (1 if expression.startswith('^') else 0):]
            if suffix.startswith(('?', '*', '{')):
                literal = literal[:-1]
            route = (prefix + literal).casefold()
            if not expression.startswith('^') or not route or 'admin/'.startswith(route) or route.startswith('admin/') or '|' in expression:
                raise ValidationError('Application regex URLs may capture reserved admin/ prefix.')


def _validate_owned_routes(patterns, package):
    from django.core.exceptions import ValidationError
    from django.urls import URLResolver
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            module = pattern.urlconf_name
            name = module if isinstance(module, str) else getattr(module, '__name__', None)
            if name and not (name == package or name.startswith(package + '.')):
                raise ValidationError('Application URL includes must belong to its package.')
            _validate_owned_routes(pattern.url_patterns, package)
        else:
            module = getattr(pattern.callback, '__module__', '')
            if not (module == package or module.startswith(package + '.')):
                raise ValidationError('Application URL callbacks must belong to its package.')


def validate_presentation(manifest) -> None:
    """Django model/route/handler checks. Only metadata is inspected, never records."""
    from django.core.exceptions import ValidationError
    from django import forms
    from django.apps import apps
    from django.db import models
    from django.urls import include, path, reverse, NoReverseMatch
    from uuid import UUID
    from skeleton.common.models import SecuredRecord
    from .registry import all_specs, get_spec
    validate_runtime_presentation(
        {'administration': asdict(manifest.administration),
         'model_ui': [asdict(item) for item in manifest.model_ui],
         'configuration_fields': [asdict(item) for item in manifest.configuration_fields]},
        manifest.app_label, error_class=ValidationError)

    def owned_spec(key):
        try:
            spec = get_spec(key)
        except KeyError as exc:
            raise ValidationError(f'{key}: presentation references an unknown model.') from exc
        if spec.model._meta.app_label != manifest.app_label or spec.scope != 'organization' or not spec.expose_api or not issubclass(spec.model, SecuredRecord):
            raise ValidationError(f'{key}: presentation requires an owned exposed SecuredRecord.')
        return spec

    declared = {item.table_key: item for item in manifest.model_ui}
    for key in declared:
        owned_spec(key)
    for spec in all_specs():
        key = spec.model._meta.label_lower
        if spec.model._meta.app_label != manifest.app_label or not spec.expose_api:
            continue
        if len(spec.write_fields) != len(set(spec.write_fields)):
            raise ValidationError(f'{key}: duplicate writable fields.')
        owned_spec(key)
        ui = declared.get(key, ModelUISpec(key))
        concrete = {field.name: field for field in spec.model._meta.concrete_fields}
        readable = set(spec.read_fields) | SYSTEM_READ_FIELDS
        for name in (*ui.list.columns, *ui.list.filters, *((ui.list.title_field,) if ui.list.title_field else ())):
            if name == '__label__' and name in ui.list.columns:
                continue
            if name not in concrete or name not in readable:
                raise ValidationError(f'{key}.{name}: list fields must be concrete and readable.')
        if ui.list.title_field:
            field = concrete[ui.list.title_field]
            if field.is_relation or isinstance(field, (models.JSONField, models.FileField)) or not _widget_allowed(field, None):
                raise ValidationError(f'{key}.{ui.list.title_field}: title field must be a readable scalar.')
        for name in ui.list.filters:
            field = concrete[name]
            if name not in set(spec.read_fields) | {'sysid', 'owner', 'visibility'} or isinstance(field, models.JSONField) or (field.is_relation and name != 'owner'):
                raise ValidationError(f'{key}.{name}: unsupported exact filter.')
        editable = []
        for section in ui.form.sections:
            for entry in section.fields:
                if entry.name not in concrete:
                    raise ValidationError(f'{key}.{entry.name}: unknown form field.')
                if entry.read_only:
                    if entry.name not in readable:
                        raise ValidationError(f'{key}.{entry.name}: read-only form field is not readable.')
                else:
                    if entry.name not in spec.write_fields:
                        raise ValidationError(f'{key}.{entry.name}: form field is not writable.')
                    editable.append(entry.name)
                if not _widget_allowed(concrete[entry.name], entry.widget):
                    raise ValidationError(f'{key}.{entry.name}: incompatible or unsupported widget.')
                if entry.rows is not None:
                    field = concrete[entry.name]
                    if entry.read_only or field.is_relation:
                        raise ValidationError('rows requires an editable textarea widget.')
                    if entry.widget is None:
                        formfield = field.formfield()
                        if formfield is None or not isinstance(formfield.widget, forms.Textarea):
                            raise ValidationError('rows requires an editable textarea widget.')
        if ui.form.sections and (len(editable) != len(set(editable)) or set(editable) != set(spec.write_fields)):
            raise ValidationError(f'{key}: form must cover every writable field exactly once.')
        for name in spec.write_fields:
            if name not in concrete or not _widget_allowed(concrete[name], None):
                raise ValidationError(f'{key}.{name}: unsupported generic form field.')
    routes = None
    if manifest.urls:
        module, app_name, namespace = include(manifest.urls)
        if app_name != manifest.app_label or namespace not in (None, manifest.app_label):
            raise ValidationError('Application URLs require their own app-label namespace.')
        patterns = module.urlpatterns if hasattr(module, 'urlpatterns') else module
        _validate_reserved_routes(patterns)
        _validate_owned_routes(patterns, apps.get_app_config(manifest.app_label).name)
        routes = (path('o/<uuid:organization_id>/apps/' + manifest.app_label + '/', include((patterns, app_name), namespace=manifest.app_label)),)
    if manifest.api_urls:
        module, app_name, namespace = include(manifest.api_urls)
        own_names = (manifest.app_label, manifest.app_label + '_api')
        if app_name not in own_names or namespace not in (None, app_name):
            raise ValidationError('Application API URLs require their own app-label namespace.')
        patterns = module.urlpatterns if hasattr(module, 'urlpatterns') else module
        _validate_owned_routes(patterns, apps.get_app_config(manifest.app_label).name)
    for entry in manifest.navigation:
        owned_spec(entry.table_key)
        try:
            if routes is None or not entry.url_name.startswith(manifest.app_label + ':'):
                raise NoReverseMatch()
            reverse(entry.url_name, urlconf=routes, kwargs={'organization_id': UUID(int=1)})
        except NoReverseMatch as exc:
            raise ValidationError('Application navigation must resolve within its own namespace.') from exc
    for screen in manifest.administration.screens:
        if screen.handler_id:
            try:
                get_admin_handler(manifest.app_label, screen.handler_id)
            except KeyError as exc:
                raise ValidationError(f'{screen.id}: administrative handler is not registered.') from exc
        if screen.url_name:
            try:
                if routes is None:
                    raise NoReverseMatch()
                reverse(screen.url_name, urlconf=routes, kwargs={'organization_id': UUID(int=1)})
            except NoReverseMatch as exc:
                raise ValidationError(f'{screen.id}: custom URL cannot resolve within its application.') from exc
