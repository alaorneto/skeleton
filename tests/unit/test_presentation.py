"""Closed v2 metadata, immutable defaults, and database-free Django UI checks."""
from copy import deepcopy
from dataclasses import replace
from types import ModuleType, SimpleNamespace
import sys

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.urls import include, path, re_path

from skeleton.system.app_registry import AppManifest, get_app, register_app, unregister_app
from skeleton.system.packages import PackageError, manifest_template, validate_manifest
from skeleton.system.presentation import (
    AdminScreenSpec, AdminSectionSpec, AdministrationSpec, FieldUISpec,
    FormLayoutSpec, FormSectionSpec, ListSpec, ModelUISpec, get_admin_handler,
    get_model_ui, presentation_from_runtime, register_admin_handler,
    FIELD_DIMENSIONS_CORE_MINIMUM, MANIFEST_V2_CORE_MINIMUM,
    required_presentation_core, validate_runtime_presentation, validate_presentation,
)


def manifest_v2():
    manifest = manifest_template(app_id='org.test.ui', app_label='contract_ui', package_name='contract_ui', publisher='test-publisher')
    manifest['runtime'].update({
        'configuration_fields': [{'name': 'heading', 'type': 'str', 'default': 'Demo', 'label': 'Heading', 'help_text': 'Tenant title', 'section_id': 'general'}],
        'administration': {
            'configuration_sections': [{'id': 'general', 'label': 'General'}],
            'sections': [{'id': 'parameters', 'label': 'Parameters'}],
            'screens': [{'id': 'configuration', 'section_id': 'parameters', 'label': 'Configuration', 'scope': 'organization', 'kind': 'configuration', 'organization_admin_only': True}],
        },
        'model_ui': [{'table_key': 'contract_ui.record', 'list': {'columns': ['title'], 'filters': ['title']},
                      'form': {'tabs': [{'id': 'main', 'label': 'Main'}], 'sections': [{'id': 'main', 'label': 'Main', 'tab_id': 'main', 'fields': [{'name': 'title', 'widget': 'text'}]}]}}],
    })
    return manifest


def test_v2_parameter_metadata_round_trips_without_changing_model_ui():
    manifest = manifest_v2()
    validate_manifest(manifest)
    admin, model_ui = presentation_from_runtime(manifest['runtime'])
    assert admin.configuration_sections == (AdminSectionSpec('general', 'General'),)
    assert admin.screens == (
        AdminScreenSpec('configuration', 'parameters', 'Configuration', 'organization', 'configuration',
                        organization_admin_only=True),
    )
    assert model_ui[0].table_key == 'contract_ui.record'
    assert model_ui[0].list == ListSpec(columns=('title',), filters=('title',))
    assert model_ui[0].form.sections[0].fields == (FieldUISpec('title', widget='text'),)



@pytest.mark.parametrize('dimension,maximum', [('col_span', 12), ('rows', 40)])
@pytest.mark.parametrize('value', [True, False, '4', 4.0, 0, -1, [], {}])
def test_dimensions_reject_non_exact_integers_and_nonpositive_values(dimension, maximum, value):
    runtime = manifest_v2()['runtime']
    field = runtime['model_ui'][0]['form']['sections'][0]['fields'][0]
    field.update(widget='textarea', **{dimension: value})
    with pytest.raises(ValueError, match=dimension):
        validate_runtime_presentation(runtime, 'contract_ui')


@pytest.mark.parametrize('dimension,maximum', [('col_span', 12), ('rows', 40)])
def test_dimension_boundaries_and_null_preserve_json(dimension, maximum):
    runtime = manifest_v2()['runtime']
    field = runtime['model_ui'][0]['form']['sections'][0]['fields'][0]
    for value in (None, 1, maximum):
        field.update(widget='textarea', **{dimension: value})
        before = deepcopy(runtime)
        validate_runtime_presentation(runtime, 'contract_ui')
        _, ui = presentation_from_runtime(runtime)
        assert getattr(ui[0].form.sections[0].fields[0], dimension) == value
        assert runtime == before
    field[dimension] = maximum + 1
    with pytest.raises(ValueError, match=dimension):
        validate_runtime_presentation(runtime, 'contract_ui')


def test_field_dimensions_preserve_positional_arguments_shorthand_and_section_columns():
    assert FieldUISpec('body', 'Body', 'Help', 'textarea', False) == FieldUISpec(
        'body', label='Body', help_text='Help', widget='textarea')
    runtime = manifest_v2()['runtime']
    section = runtime['model_ui'][0]['form']['sections'][0]
    section.update(columns=2, fields=[
        {'name': 'code', 'col_span': 4}, {'name': 'title', 'col_span': 8},
        {'name': 'body', 'widget': 'textarea', 'rows': 8}, 'other'])
    before = deepcopy(runtime)
    validate_runtime_presentation(runtime, 'contract_ui')
    _, ui = presentation_from_runtime(runtime)
    assert ui[0].form.sections[0].fields == (
        FieldUISpec('code', col_span=4), FieldUISpec('title', col_span=8),
        FieldUISpec('body', widget='textarea', rows=8), FieldUISpec('other'))
    assert runtime == before


@pytest.mark.parametrize('metadata', [
    {'read_only': True}, {'widget': 'text'}, {'widget': 'number'},
    {'widget': 'checkbox'}, {'widget': 'select'}, {'widget': 'reference'},
    {'widget': 'date'}, {'widget': 'datetime'}, {'widget': 'time'}, {'widget': 'uuid'},
])
def test_rows_reject_readonly_and_explicit_nontextarea_widgets(metadata):
    runtime = manifest_v2()['runtime']
    field = runtime['model_ui'][0]['form']['sections'][0]['fields'][0]
    field.pop('widget')
    field.update(rows=8, **metadata)
    with pytest.raises(ValueError, match='rows requires an editable textarea widget'):
        validate_runtime_presentation(runtime, 'contract_ui')


@pytest.mark.parametrize('widget', [None, 'textarea', 'json'])
def test_rows_pure_validation_accepts_textarea_or_deferred_widget(widget):
    runtime = manifest_v2()['runtime']
    runtime['model_ui'][0]['form']['sections'][0]['fields'][0].update(widget=widget, rows=8)
    validate_runtime_presentation(runtime, 'contract_ui')


@pytest.mark.parametrize('dimension', ['col_span', 'rows'])
def test_required_core_depends_on_json_key_presence_including_null(dimension):
    runtime = manifest_v2()['runtime']
    assert MANIFEST_V2_CORE_MINIMUM == '0.2.0'
    assert FIELD_DIMENSIONS_CORE_MINIMUM == '0.3.0'
    assert required_presentation_core(runtime) == '0.2.0'
    field = runtime['model_ui'][0]['form']['sections'][0]['fields'][0]
    field[dimension] = None
    before = deepcopy(runtime)
    validate_runtime_presentation(runtime, 'contract_ui')
    assert required_presentation_core(runtime) == '0.3.0'
    assert runtime == before
    runtime['model_ui'][0]['form']['sections'][0]['fields'] = ['title']
    assert required_presentation_core(runtime) == '0.2.0'


@pytest.mark.parametrize('name,widget,accepted', [
    ('code', None, False), ('code', 'textarea', True),
    ('body', None, True), ('body', 'textarea', True),
    ('payload', None, True), ('payload', 'json', True),
    ('target', None, False), ('selection', None, False),
    ('code', 'json', False), ('payload', 'textarea', False),
])
def test_rows_effective_model_widget_and_checks_are_database_free(monkeypatch, name, widget, accepted):
    from django.db import models
    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.test.utils import isolate_apps
    from skeleton.common.models import SecuredRecord
    from skeleton.system import checks, registry

    def reject_database(*args, **kwargs):
        pytest.fail('Presentation validation must not access the database.')

    monkeypatch.setattr(BaseDatabaseWrapper, 'cursor', reject_database)
    with isolate_apps():
        class DimensionRecord(SecuredRecord):
            code = models.CharField(max_length=64)
            body = models.TextField(blank=True)
            payload = models.JSONField(default=dict)
            target = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True)
            selection = models.CharField(max_length=8, choices=[('a', 'A')])

            class Meta:
                app_label = 'example_notes'

        names = ('code', 'body', 'payload', 'target', 'selection')
        spec = registry.TableSpec(DimensionRecord, 'Dimensions', read_fields=names, write_fields=names, expose_api=True)
        monkeypatch.setattr(registry, '_registry', {DimensionRecord._meta.label_lower: spec})
        entries = tuple(FieldUISpec(field, widget=widget, rows=8) if field == name else FieldUISpec(field) for field in names)
        ui = ModelUISpec(DimensionRecord._meta.label_lower, ListSpec(columns=('code',)),
                         FormLayoutSpec(sections=(FormSectionSpec('main', 'Main', entries, columns=2),)))
        manifest = replace(get_app('example_notes'), model_ui=(ui,), navigation=(), urls=None,
                           administration=AdministrationSpec(), configuration_fields=())
        monkeypatch.setattr(checks, 'all_apps', lambda: (manifest,))
        if accepted:
            validate_presentation(manifest)
            assert not any(error.id == 'skeleton.E013' for error in checks.record_contract_checks())
        else:
            message = 'rows requires an editable textarea widget' if widget is None else 'incompatible or unsupported widget'
            with pytest.raises(ValidationError, match=message):
                validate_presentation(manifest)
            assert any(error.id == 'skeleton.E013' for error in checks.record_contract_checks())


def test_null_rows_do_not_require_textarea_or_editability():
    runtime = manifest_v2()['runtime']
    field = runtime['model_ui'][0]['form']['sections'][0]['fields'][0]
    field.update(rows=None, read_only=True, widget='text')
    validate_runtime_presentation(runtime, 'contract_ui')


def test_readonly_rows_surface_as_existing_presentation_check(monkeypatch):
    from skeleton.system import checks
    manifest = get_app('example_notes')
    ui = replace(get_model_ui('example_notes.note'), form=FormLayoutSpec(sections=(
        FormSectionSpec('main', 'Main', (
            FieldUISpec('code'), FieldUISpec('title'),
            FieldUISpec('body', widget='textarea', read_only=True, rows=8))),
    )))
    manifest = replace(manifest, model_ui=(ui,))
    with pytest.raises(ValidationError, match='rows requires an editable textarea widget'):
        validate_presentation(manifest)
    monkeypatch.setattr(checks, 'all_apps', lambda: (manifest,))
    errors = checks.record_contract_checks()
    assert any(error.id == 'skeleton.E013' and
               'rows requires an editable textarea widget' in error.msg for error in errors)


@pytest.mark.parametrize('kind,scope,metadata', [
    ('model_list', 'organization', {}),
    ('configuration', 'organization', {'table_key': 'contract_ui.record'}),
    ('configuration', 'organization', {'action': 'read'}),
    ('custom', 'organization', {'table_key': 'contract_ui.record'}),
    ('custom', 'organization', {'action': 'update'}),
    ('custom', 'platform', {'table_key': 'contract_ui.record'}),
    ('custom', 'platform', {'action': 'read'}),
])
def test_admin_screens_reject_business_table_declarations(kind, scope, metadata):
    manifest = manifest_v2()
    screen = manifest['runtime']['administration']['screens'][0]
    screen.update(kind=kind, scope=scope, **metadata)
    if kind == 'custom':
        if scope == 'organization':
            screen['url_name'] = 'contract_ui:parameters'
        else:
            screen.update(handler_id='parameters', organization_admin_only=False)
    with pytest.raises(PackageError):
        validate_manifest(manifest)


@pytest.mark.parametrize('removed', [{'table_key': 'contract_ui.record'}, {'action': 'read'}])
def test_code_owned_admin_screen_cannot_accept_table_metadata(removed):
    with pytest.raises(TypeError):
        AdminScreenSpec('configuration', 'parameters', 'Configuration', 'organization', 'configuration', **removed)


@pytest.mark.parametrize('attack', [
    'runtime-unknown', 'model-unknown', 'foreign-table', 'duplicate-model',
    'duplicate-section', 'unknown-section', 'configuration-section', 'configuration-label',
    'configuration-help', 'configuration-control', 'screen-limit', 'model-limit', 'field-limit',
    'external-url', 'foreign-url', 'handler-path', 'tenant-handler', 'platform-table',
    'custom-no-authorization', 'custom-table-authorization', 'flag-type', 'unknown-widget', 'field-expression',
    'duplicate-field', 'unknown-tab', 'duplicate-tab', 'invalid-columns', 'label-limit',
    'help-limit', 'old-core', 'v1-new-runtime',
])
def test_v2_rejects_unsafe_specs_before_import(attack):
    manifest = manifest_v2()
    runtime = manifest['runtime']
    administration = runtime['administration']
    screen = administration['screens'][0]
    ui = runtime['model_ui'][0]
    section = ui['form']['sections'][0]
    field = section['fields'][0]
    if attack == 'runtime-unknown':
        runtime['template'] = 'outside/index.html'
    elif attack == 'model-unknown':
        ui['callback'] = 'outside.run'
    elif attack == 'foreign-table':
        ui['table_key'] = 'other.record'
    elif attack == 'duplicate-model':
        runtime['model_ui'].append(deepcopy(ui))
    elif attack == 'duplicate-section':
        administration['sections'].append(deepcopy(administration['sections'][0]))
    elif attack == 'unknown-section':
        screen['section_id'] = 'missing'
    elif attack == 'configuration-section':
        runtime['configuration_fields'][0]['section_id'] = 'missing'
    elif attack == 'configuration-label':
        runtime['configuration_fields'][0]['label'] = 'x' * 201
    elif attack == 'configuration-help':
        runtime['configuration_fields'][0]['help_text'] = 'x' * 2001
    elif attack == 'configuration-control':
        runtime['configuration_fields'][0]['name'] = 'revision'
    elif attack == 'screen-limit':
        administration['screens'] = [dict(screen, id=f'screen_{index}') for index in range(101)]
    elif attack == 'model-limit':
        runtime['model_ui'] = [dict(ui, table_key=f'contract_ui.record_{index}') for index in range(201)]
    elif attack == 'field-limit':
        section['fields'] = [f'field_{index}' for index in range(201)]
    elif attack in ('external-url', 'foreign-url', 'custom-no-authorization', 'custom-table-authorization'):
        screen.update(kind='custom', url_name={'external-url': 'https://evil.test/', 'foreign-url': 'other:home',
                      'custom-no-authorization': 'contract_ui:parameters',
                      'custom-table-authorization': 'contract_ui:parameters'}[attack])
        if attack in ('custom-no-authorization', 'custom-table-authorization'):
            screen['organization_admin_only'] = False
        if attack == 'custom-table-authorization':
            screen['table_key'] = 'contract_ui.record'
    elif attack == 'handler-path':
        screen.update(scope='platform', kind='custom', handler_id='outside.run', organization_admin_only=False)
    elif attack == 'tenant-handler':
        screen['handler_id'] = 'run'
    elif attack == 'platform-table':
        screen.update(scope='platform', kind='custom', handler_id='run',
                      organization_admin_only=False, table_key='contract_ui.record')
    elif attack == 'flag-type':
        screen['sensitive'] = 1
    elif attack == 'unknown-widget':
        field['widget'] = '<script>'
    elif attack == 'field-expression':
        field['name'] = 'owner.email'
    elif attack == 'duplicate-field':
        section['fields'].append('title')
    elif attack == 'unknown-tab':
        section['tab_id'] = 'missing'
    elif attack == 'duplicate-tab':
        ui['form']['tabs'].append(deepcopy(ui['form']['tabs'][0]))
    elif attack == 'invalid-columns':
        section['columns'] = True
    elif attack == 'label-limit':
        section['label'] = 'x' * 201
    elif attack == 'help-limit':
        field['help_text'] = 'x' * 2001
    elif attack == 'old-core':
        manifest['compatibility']['skeleton'] = '>=0.1.0,<1.0.0'
    elif attack == 'v1-new-runtime':
        manifest['manifest_version'] = 1
    with pytest.raises(PackageError):
        validate_manifest(manifest)




@pytest.mark.parametrize('invalid', ['unknown-field', 'unreadable-column', 'relation-title', 'incomplete-form', 'incompatible-widget', 'readonly-write-field', 'unknown-model'])
def test_django_checks_fields_coverage_and_widgets_without_database(invalid):
    manifest = get_app('example_notes')
    ui = get_model_ui('example_notes.note')
    if invalid == 'unknown-field':
        ui = replace(ui, list=ListSpec(columns=('missing',)))
    elif invalid == 'unreadable-column':
        ui = replace(ui, list=ListSpec(columns=('organization',)))
    elif invalid == 'relation-title':
        ui = replace(ui, list=replace(ui.list, title_field='owner'))
    elif invalid == 'incomplete-form':
        ui = replace(ui, form=FormLayoutSpec(sections=(FormSectionSpec('main', 'Main', (FieldUISpec('code'), FieldUISpec('title'))),)))
    elif invalid == 'incompatible-widget':
        ui = replace(ui, form=FormLayoutSpec(sections=(FormSectionSpec('main', 'Main', (FieldUISpec('code'), FieldUISpec('title'), FieldUISpec('body', widget='checkbox'))),)))
    elif invalid == 'readonly-write-field':
        ui = replace(ui, form=FormLayoutSpec(sections=(FormSectionSpec('main', 'Main', (FieldUISpec('code'), FieldUISpec('title'), FieldUISpec('body', read_only=True))),)))
    elif invalid == 'unknown-model':
        ui = replace(ui, table_key='example_notes.missing')
    with pytest.raises(ValidationError):
        validate_presentation(replace(manifest, model_ui=(ui,)))


def test_static_checks_report_presentation_failure(monkeypatch):
    from skeleton.system import checks
    manifest = get_app('example_notes')
    ui = replace(get_model_ui('example_notes.note'), list=ListSpec(columns=('missing',)))
    monkeypatch.setattr(checks, 'all_apps', lambda: (replace(manifest, model_ui=(ui,)),))
    errors = checks.record_contract_checks()
    assert any(error.id == 'skeleton.E013' for error in errors)


def test_registration_removes_ui_and_owned_handlers_on_unregister(monkeypatch):
    manifest = AppManifest('presentation_fixture', 'Fixture', 'Contract', model_ui=(ModelUISpec('presentation_fixture.record'),))
    register_app(manifest)
    monkeypatch.setattr(apps, 'get_app_config', lambda label: SimpleNamespace(name='presentation_fixture'))
    def handler(request):
        return request
    handler.__module__ = 'presentation_fixture.handlers'
    def other(request):
        return request
    other.__module__ = 'presentation_fixture.handlers'
    def invalid_signature(request, required_argument):
        return request
    invalid_signature.__module__ = 'presentation_fixture.handlers'
    try:
        assert register_admin_handler(manifest.app_label, 'overview', handler) is handler
        assert get_admin_handler(manifest.app_label, 'overview') is handler
        with pytest.raises(ValidationError):
            register_admin_handler(manifest.app_label, 'overview', other)
        with pytest.raises(ValidationError):
            register_admin_handler(manifest.app_label, 'foreign', test_registration_removes_ui_and_owned_handlers_on_unregister)
        with pytest.raises(ValidationError):
            register_admin_handler(manifest.app_label, 'invalid_signature', invalid_signature)
        assert get_model_ui('presentation_fixture.record') is manifest.model_ui[0]
    finally:
        unregister_app(manifest.app_label)
    assert get_model_ui('presentation_fixture.record') is not manifest.model_ui[0]
    with pytest.raises(KeyError):
        get_admin_handler(manifest.app_label, 'overview')
    with pytest.raises(ValidationError):
        register_admin_handler(manifest.app_label, 'overview', handler)


def route_manifest(monkeypatch, patterns):
    manifest = get_app('example_notes')
    module_name = apps.get_app_config(manifest.app_label).name + '.presentation_test_urls'
    module = ModuleType(module_name)
    module.app_name = manifest.app_label
    module.urlpatterns = patterns
    monkeypatch.setitem(sys.modules, module_name, module)
    return replace(manifest, urls=module_name, navigation=(), configuration_fields=(), administration=AdministrationSpec())


def owned_callback(request, organization_id=None):
    return request


owned_callback.__module__ = 'examples.notes.views'


@pytest.mark.parametrize('pattern', [
    path('admin/', owned_callback), path('admin/screens/<str:screen_id>/', owned_callback),
    path('<str:page>/', owned_callback), path('<path:page>', owned_callback),
    re_path(r'^.*$', owned_callback), re_path(r'^n?admin/', owned_callback),
    path('', include([path('admin/', owned_callback)])),
])
def test_reserved_admin_prefix_collisions_fail(monkeypatch, pattern):
    manifest = route_manifest(monkeypatch, [pattern])
    with pytest.raises(ValidationError):
        validate_presentation(manifest)


def test_custom_route_resolves_only_in_own_namespace(monkeypatch):
    manifest = route_manifest(monkeypatch, [path('operation/', owned_callback, name='operation')])
    admin = AdministrationSpec(sections=(AdminSectionSpec('operations', 'Operations'),), screens=(
        AdminScreenSpec('operation', 'operations', 'Operation', 'organization', 'custom',
                        url_name='example_notes:operation', organization_admin_only=True),))
    validate_presentation(replace(manifest, administration=admin))
    broken = replace(admin.screens[0], url_name='example_notes:missing')
    with pytest.raises(ValidationError):
        validate_presentation(replace(manifest, administration=replace(admin, screens=(broken,))))


def test_platform_screen_requires_registered_owned_handler():
    manifest = get_app('example_notes')
    admin = AdministrationSpec(configuration_sections=manifest.administration.configuration_sections, sections=(AdminSectionSpec('operations', 'Operations'),), screens=(
        AdminScreenSpec('operation', 'operations', 'Operation', 'platform', 'custom', handler_id='unregistered_operation'),))
    with pytest.raises(ValidationError):
        validate_presentation(replace(manifest, administration=admin))




def test_foreign_url_callback_is_rejected(monkeypatch):
    def foreign_callback(request):
        return request
    manifest = route_manifest(monkeypatch, [path('foreign/', foreign_callback)])
    with pytest.raises(ValidationError):
        validate_presentation(manifest)


def test_default_layout_rejects_upload_field_without_database(monkeypatch):
    from django.db import models
    from django.test.utils import isolate_apps
    from skeleton.common.models import SecuredRecord
    from skeleton.system import registry
    with isolate_apps():
        class Document(SecuredRecord):
            upload = models.FileField()

            class Meta:
                app_label = 'example_notes'
        spec = registry.TableSpec(Document, 'Dedicated upload', read_fields=('upload',), write_fields=('upload',), expose_api=True)
        monkeypatch.setattr(registry, '_registry', {Document._meta.label_lower: spec})
        manifest = replace(get_app('example_notes'), model_ui=(), navigation=(), urls=None, administration=AdministrationSpec())
        with pytest.raises(ValidationError):
            validate_presentation(manifest)


def test_api_foreign_callback_is_rejected(monkeypatch):
    def foreign_callback(request):
        return request
    manifest = route_manifest(monkeypatch, [path('operation/', foreign_callback, name='operation')])
    module = sys.modules[manifest.urls]
    module.app_name = manifest.app_label + '_api'
    api_manifest = replace(manifest, urls=None, api_urls=manifest.urls)
    with pytest.raises(ValidationError):
        validate_presentation(api_manifest)


def test_nullable_boolean_default_is_supported_but_checkbox_is_not(monkeypatch):
    from django.db import models
    from django.test.utils import isolate_apps
    from skeleton.common.models import SecuredRecord
    from skeleton.system import registry
    with isolate_apps():
        class NullableRecord(SecuredRecord):
            active = models.BooleanField(null=True)
            payload = models.JSONField(default=dict)

            class Meta:
                app_label = 'example_notes'
        key = NullableRecord._meta.label_lower
        spec = registry.TableSpec(NullableRecord, 'Nullable', read_fields=('active', 'payload'), write_fields=('active', 'payload'), expose_api=True)
        monkeypatch.setattr(registry, '_registry', {key: spec})
        manifest = replace(get_app('example_notes'), model_ui=(), navigation=(), urls=None, api_urls=None, configuration_fields=(), administration=AdministrationSpec())
        validate_presentation(manifest)
        layout = FormLayoutSpec(sections=(FormSectionSpec('main', 'Main', (FieldUISpec('active', widget='checkbox'), FieldUISpec('payload'))),))
        with pytest.raises(ValidationError):
            validate_presentation(replace(manifest, model_ui=(ModelUISpec(key, form=layout),)))
        with pytest.raises(ValidationError):
            validate_presentation(replace(manifest, model_ui=(ModelUISpec(key, list=ListSpec(title_field='payload')),)))


def test_nullable_boolean_form_and_gateway_agree_on_null_and_reject_coercion():
    from django.db import models
    from django.test.utils import isolate_apps
    from skeleton.common.models import SecuredRecord
    from skeleton.system.registry import TableSpec
    from skeleton.web.forms import record_form, wire_values
    from skeleton.web.services import coerce_fields
    with isolate_apps():
        class NullableInput(SecuredRecord):
            active = models.BooleanField(null=True, blank=True)
            strict = models.BooleanField(default=False)

            class Meta:
                app_label = 'example_notes'
        spec = TableSpec(NullableInput, 'Nullable input', read_fields=('active',), write_fields=('active',), expose_api=True)
        form = record_form(spec, user=None, data={'active': 'unknown'})
        assert form.is_valid(), form.errors
        assert coerce_fields(NullableInput, wire_values(form.cleaned_data), user=None)['active'] is None
        assert coerce_fields(NullableInput, {'active': False}, user=None)['active'] is False
        for values in ({'strict': None}, {'active': 'false'}, {'active': 1}):
            with pytest.raises(ValidationError):
                coerce_fields(NullableInput, values, user=None)


@pytest.mark.parametrize('span,columns,wide,expected', [
    (None, 1, False, 12), (None, 2, False, 6), (None, 2, True, 12),
    (4, 2, False, 4), (8, 2, True, 8), (1, 1, True, 1), (12, 2, False, 12),
])
def test_resolved_grid_explicit_dimensions_override_auto_width(span, columns, wide, expected):
    from skeleton.web.presentation import resolve_field_span
    assert resolve_field_span(span, columns, wide=wide) == expected


@pytest.mark.parametrize('span,columns,wide', [
    (True, 2, False), ('4', 2, False), (4.0, 2, False), (0, 2, False),
    (13, 2, False), (None, True, False), (None, 3, False), (None, 2, 1),
])
def test_resolved_grid_rejects_invalid_dimensions(span, columns, wide):
    from skeleton.web.presentation import resolve_field_span
    with pytest.raises(ValueError):
        resolve_field_span(span, columns, wide=wide)
