"""Application groups share authorization with their executable routes."""
from dataclasses import replace
import re

import pytest
from django.http import HttpResponse
from django.test import Client, override_settings
from django.urls import include, path, resolve, reverse

from skeleton.accounts.services import bootstrap_identity
from skeleton.system import app_registry, presentation
from skeleton.system.applications import application_state, configure_application
from skeleton.system.presentation import AdministrationSpec, AdminScreenSpec, AdminSectionSpec
from skeleton.tenancy.context import system_context, tenant_context

pytestmark = pytest.mark.usefixtures('skeleton_db')
PASSWORD = 'Application-administration-Password!54'


@pytest.fixture
def actor():
    user, organization = bootstrap_identity(email='administration@example.test', organization='administration',
                                            name='Administration', password=PASSWORD)
    with system_context(reason='test:organization-only-administrator'):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=['is_staff', 'is_superuser'])
    client = Client(enforce_csrf_checks=True)
    client.get(reverse('account_login'))
    assert client.post(reverse('account_login'), {'login': user.email, 'password': PASSWORD,
                       'csrfmiddlewaretoken': client.cookies['csrftoken'].value}).status_code == 302
    user.refresh_from_db()
    return user, organization, client


@pytest.fixture
def app_screens(monkeypatch):
    original = app_registry.get_app('example_notes')
    metadata = AdministrationSpec(
        configuration_sections=(AdminSectionSpec('general', 'General configuration'),),
        sections=(AdminSectionSpec('parameters', 'Parameters'),),
        screens=(
            AdminScreenSpec('configuration', 'parameters', 'Application configuration', 'organization', 'configuration',
                            organization_admin_only=True),
            AdminScreenSpec('platform', 'parameters', 'Platform screen', 'platform', 'custom', handler_id='test_handler'),
        ),
    )
    fields = tuple(replace(field, label='Tenant heading', help_text='Displayed in tenant notes', section_id='general')
                   for field in original.configuration_fields)
    changed = replace(original, administration=metadata, configuration_fields=fields)
    monkeypatch.setitem(app_registry._manifests, original.app_label, changed)
    return changed


@pytest.fixture
def member_actor(actor):
    from skeleton.access.models import MemberRole, Role, TablePermission
    from skeleton.system.models import TableDefinition
    from skeleton.tenancy.models import Membership
    owner, organization, _ = actor
    member, _ = bootstrap_identity(email='administration-member@example.test', organization='administration-member',
                                   name='Member organization', password=PASSWORD)
    with tenant_context(organization_id=organization.pk, actor=owner, source='test:admin-screen-member'):
        membership = Membership.objects.create(organization=organization, user=member)
        role = Role.objects.get(code='member')
        MemberRole.objects.create(organization=organization, membership=membership, role=role)
        TablePermission.objects.create(organization=organization, role=role,
                                       table=TableDefinition.objects.get(model_label='example_notes.note'), action='read')
    client = Client()
    assert client.post(reverse('account_login'), {'login': member.email, 'password': PASSWORD}).status_code == 302
    return member, client


def platform(user):
    with system_context(reason='test:administration-platform'):
        user.refresh_from_db()
        user.is_staff = user.is_superuser = True
        user.save(update_fields=['is_staff', 'is_superuser'])


def test_notes_parameter_pages_do_not_offer_business_shortcuts(actor):
    _, organization, client = actor
    values = {'organization_id': organization.pk, 'app_label': 'example_notes'}
    business_url = reverse('example_notes:home', kwargs={'organization_id': organization.pk})
    records_url = reverse('web:records', kwargs={'organization_id': organization.pk, 'table_key': 'example_notes.note'})
    for name in ('application-admin', 'application-configuration'):
        response = client.get(reverse(name, kwargs=values))
        assert response.status_code == 200
        assert response.context['parameter_panel'] is True
        links = re.findall(r'href="([^"]+)"', response.content.decode())
        assert business_url not in links
        assert records_url not in links
    group = client.get(reverse('application-admin', kwargs=values))
    assert [screen['id'] for section in group.context['sections'] for screen in section['screens']] == ['configuration']
    assert client.get(records_url).status_code == 200


def test_disabled_app_retains_parameters_but_blocks_business_routes(actor, app_screens):
    user, organization, client = actor
    with tenant_context(organization_id=organization.pk, actor=user, source='test:disable-admin-app'):
        configure_application('example_notes', user=user, enabled=False, configuration={'heading': 'Saved heading'}, revision=0)
    base = f'/o/{organization.pk}/apps/example_notes/'
    group = client.get(base + 'admin/')
    assert group.status_code == 200
    assert [[screen['id'] for screen in section['screens']] for section in group.context['sections']] == [['configuration']]
    assert 'models' not in group.context
    assert not any(url.startswith(f'/o/{organization.pk}/tables/') for url in re.findall(r'href="([^"]+)"', group.content.decode()))
    assert client.get(base + 'admin/screens/records/').status_code == 404
    assert client.get(base).status_code == 403
    assert client.get(base + 'admin/screens/configuration/').url == base + 'configuration/'
    configuration = client.get(base + 'configuration/')
    assert configuration.status_code == 200
    form = configuration.context['form']
    assert form['heading'].value() == 'Saved heading'
    assert form.fields['heading'].label == 'Tenant heading'
    assert form.fields['heading'].help_text == 'Displayed in tenant notes'
    assert [section['id'] for section in configuration.context['sections']] == ['application-activation', 'general']
    assert client.post(base + 'configuration/', {
        'enabled': 'on', 'revision': '1', 'heading': 'Saved heading',
        'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
    }).status_code == 302
    assert client.get(base + 'admin/screens/records/').status_code == 404
    assert client.get(base).status_code == 200
    assert client.get(f'/o/{organization.pk}/tables/example_notes.note/').status_code == 200


def test_configuration_post_keeps_compare_and_swap_and_csrf(actor, app_screens):
    user, organization, client = actor
    url = f'/o/{organization.pk}/apps/example_notes/configuration/'
    assert client.post(url, {'enabled': 'on', 'revision': '0', 'heading': 'CSRF missing'}).status_code == 403
    client.get(url)
    values = {'enabled': 'on', 'revision': '0', 'heading': 'First',
              'csrfmiddlewaretoken': client.cookies['csrftoken'].value}
    assert client.post(url, values).status_code == 302
    assert client.post(url, {**values, 'heading': 'Stale'}).status_code == 409
    with tenant_context(organization_id=organization.pk, actor=user, source='test:read-admin-config'):
        assert application_state('example_notes')['configuration']['heading'] == 'First'


def test_platform_groups_are_unique_and_removed_with_registry(actor, app_screens, monkeypatch):
    user, organization, client = actor
    platform(user)
    index = client.get(reverse('admin:index'))
    assert index.status_code == 200
    groups = [group for group in index.context['app_list'] if group.get('app_id') == app_screens.app_id]
    assert len(groups) == 1
    assert app_registry.get_app('example_notes').label.encode() in index.content
    group = client.get(reverse('admin:skeleton_app_group', kwargs={'app_label': app_screens.app_label}))
    assert group.status_code == 200
    assert 'models' not in group.context
    assert [screen.id for screen in group.context['tenant_screens']] == ['configuration']
    assert not any('/tables/' in url for url in re.findall(r'href="([^"]+)"', group.content.decode()))
    monkeypatch.delitem(app_registry._manifests, 'example_notes')
    assert client.get('/admin/system/apps/example_notes/').status_code == 404
    assert client.get('/admin/system/apps/example_notes/screens/platform/').status_code == 404
    assert client.get(f'/o/{organization.pk}/apps/example_notes/').status_code == 404
    refreshed = client.get(reverse('admin:index'))
    assert not any(group.get('app_id') == app_screens.app_id for group in refreshed.context['app_list'])


def test_table_read_permission_does_not_grant_parameter_panel(actor, app_screens, member_actor):
    _, organization, _ = actor
    _, client = member_actor
    base = f'/o/{organization.pk}/apps/example_notes/'
    assert client.get(base + 'admin/').status_code == 403
    assert client.get(base + 'admin/screens/configuration/').status_code == 403
    assert client.get(base + 'configuration/').status_code == 403
    assert client.get(base).status_code == 200
    assert client.get(f'/o/{organization.pk}/catalog/').status_code == 200
    assert client.get(f'/o/{organization.pk}/tables/example_notes.note/').status_code == 200


def test_custom_parameter_wrapper_guards_direct_route_without_activation_gate(actor, app_screens, member_actor, monkeypatch):
    from django.core.exceptions import PermissionDenied
    from django.test import RequestFactory
    from skeleton.system.administration import guard_application_urls, screen_sections
    from skeleton.system.application_views import application_configuration
    user, organization, client = actor
    member, member_client = member_actor
    parameter = AdminScreenSpec('custom', 'parameters', 'Custom parameters', 'organization', 'custom',
                                url_name='example_notes:parameters', organization_admin_only=True)
    manifest = replace(app_screens, administration=replace(
        app_screens.administration, screens=(*app_screens.administration.screens, parameter)))
    monkeypatch.setitem(app_registry._manifests, manifest.app_label, manifest)
    original = path('parameters/', application_configuration, {'app_label': manifest.app_label}, name='parameters')
    guarded = guard_application_urls([original], manifest)
    routes = (path('', include((guarded, manifest.app_label), namespace=manifest.app_label)),)
    request = RequestFactory().get('/parameters/')
    request.resolver_match = resolve('/parameters/', urlconf=routes)
    request.user, request.session = user, client.session
    with tenant_context(organization_id=organization.pk, actor=user, source='test:disabled-custom-parameters'):
        configure_application(manifest.app_label, user=user, enabled=False, configuration={'heading': 'Saved'}, revision=0)
        sections = screen_sections(manifest, user=user, scope='organization')
        assert [screen['id'] for screen in sections[0]['screens']] == ['configuration', 'custom']
        response = request.resolver_match.func(request, organization_id=organization.pk, **request.resolver_match.kwargs)
        assert response.status_code == 200
        assert original.callback is application_configuration
    request.user, request.session = member, member_client.session
    with tenant_context(organization_id=organization.pk, actor=member, source='test:custom-parameters-member'):
        assert screen_sections(manifest, user=member, scope='organization') == []
        with pytest.raises(PermissionDenied):
            request.resolver_match.func(request, organization_id=organization.pk, **request.resolver_match.kwargs)
    assert client.get(f'/o/{organization.pk}/apps/example_notes/').status_code == 403


def test_platform_membership_selection_does_not_grant_foreign_tenant(actor, app_screens):
    user, organization, client = actor
    _, foreign = bootstrap_identity(email='foreign-administration@example.test', organization='foreign-administration',
                                    name='Foreign administration', password=PASSWORD)
    platform(user)
    selection = client.get('/admin/system/apps/example_notes/organizations/')
    assert selection.status_code == 200
    assert str(organization.pk).encode() in selection.content
    assert str(foreign.pk).encode() not in selection.content
    assert client.get(f'/o/{foreign.pk}/apps/example_notes/admin/').status_code == 404
    assert client.get(f'/o/{foreign.pk}/apps/example_notes/configuration/').status_code == 404
    assert client.get('/admin/system/apps/example_notes/organizations/?screen=https://example.test').status_code == 404


def test_documentation_is_escaped_and_not_client_selected(actor, app_screens, tmp_path, monkeypatch):
    user, _, client = actor
    platform(user)
    (tmp_path / 'spec.md').write_text('<script>alert("package")</script>', encoding='utf-8')
    (tmp_path / 'adr.md').write_text('Decision', encoding='utf-8')
    monkeypatch.setitem(app_registry._manifests, 'example_notes', replace(app_screens, documentation_root=str(tmp_path)))
    response = client.get('/admin/system/apps/example_notes/documentation/', {'path': '/etc/passwd'})
    assert response.status_code == 200
    assert b'&lt;script&gt;' in response.content and b'<script>alert(' not in response.content
    assert b'root:x:' not in response.content


def test_platform_handler_has_admin_mfa_csrf_and_sensitive_reauth(actor, app_screens, monkeypatch):
    from django.apps import apps
    user, _, client = actor
    invoked = []

    def handler(request):
        invoked.append(request.method)
        return HttpResponse('Owned handler')

    handler.__module__ = apps.get_app_config('example_notes').name + '.admin_handlers'
    monkeypatch.setattr(presentation, '_handlers', dict(presentation._handlers))
    presentation.register_admin_handler('example_notes', 'test_handler', handler)
    url = '/admin/system/apps/example_notes/screens/platform/'
    assert client.get(url).status_code == 403
    platform(user)
    with override_settings(SKELETON_REQUIRE_ADMIN_MFA=True):
        assert client.get(url).status_code == 302
    assert invoked == []
    assert client.get(url).content == b'Owned handler'
    assert client.post(url).status_code == 403
    sensitive = replace(app_screens.administration.screens[-1], sensitive=True)
    monkeypatch.setitem(app_registry._manifests, 'example_notes', replace(app_screens, administration=replace(
        app_screens.administration, screens=(*app_screens.administration.screens[:-1], sensitive))))
    with override_settings(ACCOUNT_REAUTHENTICATION_TIMEOUT=0):
        assert client.get(url).status_code == 302
    assert invoked == ['GET']


def test_removed_screen_dispatch_returns_not_found(actor, app_screens):
    _, organization, client = actor
    assert client.get(f'/o/{organization.pk}/apps/example_notes/admin/screens/removed/').status_code == 404
    assert client.post(f'/o/{organization.pk}/apps/example_notes/admin/screens/configuration/', {
        'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
    }).status_code == 405


def test_api_namespace_wrapper_uses_owning_app_activation(actor, app_screens, monkeypatch):
    from django.core.exceptions import PermissionDenied
    from django.http import Http404
    from django.test import RequestFactory
    from examples.notes.views import SummaryView
    from skeleton.system.administration import guard_application_urls
    user, organization, _ = actor
    summary = SummaryView.as_view()
    original = path('summary/', summary, name='summary')
    guarded = guard_application_urls([original], app_screens)
    routes = (path('', include((guarded, 'example_notes_api'), namespace='example_notes_api')),)
    request = RequestFactory().get('/summary/')
    request.user = user
    request.resolver_match = resolve('/summary/', urlconf=routes)
    with tenant_context(organization_id=organization.pk, actor=user, source='test:api-wrapper'):
        response = request.resolver_match.func(request, organization_id=organization.pk)
        assert response.status_code == 200
        assert response.data['count'] == 0
        assert original.callback is summary
        configure_application('example_notes', user=user, enabled=False, configuration={}, revision=0)
        with pytest.raises(PermissionDenied):
            request.resolver_match.func(request, organization_id=organization.pk)
        monkeypatch.delitem(app_registry._manifests, 'example_notes')
        with pytest.raises(Http404):
            request.resolver_match.func(request, organization_id=organization.pk)
