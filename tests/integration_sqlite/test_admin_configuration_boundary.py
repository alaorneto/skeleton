"""One secure site, separate configuration and global-record URL/shell boundaries."""
import pytest
from django.apps import apps
from django.contrib import admin
from django.test import Client, RequestFactory, override_settings
from django.urls import Resolver404, resolve, reverse

from skeleton.accounts.models import User
from skeleton.accounts.services import bootstrap_identity
from skeleton.common.models import SecuredRecord
from skeleton.tenancy.context import system_context

pytestmark = pytest.mark.usefixtures('skeleton_db')
PASSWORD = 'Mosaic-47!River-Pebble'


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse('account_login')).status_code == 200
    assert client.post(reverse('account_login'), {
        'login': user.email, 'password': PASSWORD,
        'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
    }).status_code == 302
    user.refresh_from_db()
    return client


@pytest.fixture
def platform_actor():
    user, organization = bootstrap_identity(
        email='configuration-boundary@example.test', organization='configuration-boundary',
        name='Configuration boundary', password=PASSWORD,
    )
    return user, organization, login(user)


def test_single_namespace_routes_configuration_and_registered_models_separately(platform_actor):
    user, _, client = platform_actor
    assert reverse('admin:index') == '/admin/'
    assert reverse('admin:records_index') == '/platform/records/'
    for name in ('login', 'logout', 'password_change', 'password_change_done'):
        assert reverse(f'admin:{name}').startswith('/admin/')
    assert reverse('admin:skeleton_releases') == '/admin/system/releases/'
    assert reverse('admin:skeleton_app_group', kwargs={'app_label': 'sk_system'}) == '/admin/system/apps/sk_system/'
    for name in ('jsi18n', 'autocomplete'):
        assert reverse(f'admin:{name}').startswith('/platform/records/')
    for model in admin.site._registry:
        prefix = f'{model._meta.app_label}_{model._meta.model_name}'
        expected = f'/platform/records/{model._meta.app_label}/{model._meta.model_name}/'
        assert reverse(f'admin:{prefix}_changelist') == expected
        assert reverse(f'admin:{prefix}_add') == expected + 'add/'
        assert reverse('admin:app_list', kwargs={'app_label': model._meta.app_label}) == f'/platform/records/{model._meta.app_label}/'
        for suffix in ('change', 'delete', 'history'):
            url = reverse(f'admin:{prefix}_{suffix}', args=[user.pk])
            assert url == expected + f'{user.pk}/{suffix}/'
            assert resolve(url).namespace == 'admin'
        assert resolve(expected).func.model_admin.admin_site is admin.site._wrapped
        for suffix in ('', 'add/', f'{user.pk}/change/', f'{user.pk}/delete/', f'{user.pk}/history/'):
            legacy = expected.replace('/platform/records/', '/admin/', 1) + suffix
            assert client.get(legacy).status_code == 404
            assert Client().get(legacy).status_code == 404
        assert client.get(f'/admin/{model._meta.app_label}/').status_code == 404


def test_configuration_context_contains_no_registered_model_groups_or_recent_records(platform_actor):
    user, _, client = platform_actor
    records = client.get(reverse('admin:sk_accounts_user_changelist'))
    assert records.status_code == 200
    configuration = client.get(reverse('admin:index'))
    assert configuration.status_code == 200
    labels = {model._meta.app_label for model in admin.site._registry}
    assert all(group['app_label'] not in labels for group in configuration.context['app_list'])
    assert all(group['app_label'].startswith('application__') for group in configuration.context['app_list'])
    assert all(group['app_label'] not in labels for group in configuration.context['available_apps'])
    assert b'recent-actions-module' not in configuration.content
    request = RequestFactory().get(reverse('admin:index'))
    request.user = user
    assert admin.site.get_app_list(request, 'sk_accounts') == []
    assert admin.site.get_record_app_list(request, 'sk_accounts')[0]['app_label'] == 'sk_accounts'


def test_data_index_and_model_screens_use_web_shell_and_all_required_media(platform_actor):
    user, organization, client = platform_actor
    index = client.get(reverse('admin:records_index'))
    assert index.status_code == 200
    assert b'class="platform-records ' in index.content
    assert b'aria-label="Principal"' in index.content
    assert b'id="nav-sidebar"' not in index.content
    assert b'Configura\xc3\xa7\xc3\xb5es da plataforma' not in index.content
    groups = index.context['app_list']
    assert groups and all(not group['app_label'].startswith('application__') for group in groups)
    for group in groups:
        for model in group['models']:
            assert model['admin_url'].startswith('/platform/records/')
            assert model['admin_url'].encode() in index.content
    app_index = client.get(reverse('admin:app_list', kwargs={'app_label': 'sk_accounts'}))
    assert app_index.status_code == 200
    assert {group['app_label'] for group in app_index.context['app_list']} == {'sk_accounts'}
    changelist = client.get(reverse('admin:sk_accounts_user_changelist'))
    assert changelist.status_code == 200
    for asset in ('admin/css/base.css', 'admin/css/changelists.css', 'admin/js/filters.js'):
        assert asset.encode() in changelist.content
    for name, args in (
        ('sk_accounts_user_add', []), ('sk_accounts_user_change', [user.pk]),
        ('sk_tenancy_organization_add', []), ('sk_tenancy_organization_change', [organization.pk]),
    ):
        form = client.get(reverse(f'admin:{name}', args=args))
        assert form.status_code == 200
        assert b'class="platform-records ' in form.content
        assert b'id="nav-sidebar"' not in form.content
        for asset in ('admin/css/forms.css', 'admin/js/core.js', 'admin/js/jquery.init.js', 'admin/js/change_form.js'):
            assert asset.encode() in form.content
        assert reverse('admin:jsi18n').encode() in form.content
        assert b'data-admin-utc-offset=' in form.content
        assert b'csrfmiddlewaretoken' in form.content


def test_app_index_and_catch_all_do_not_capture_unrelated_root_urls(platform_actor):
    _, organization, client = platform_actor
    assert resolve('/accounts/login/').view_name == 'account_login'
    assert resolve('/').view_name == 'web:home'
    assert resolve('/api/v1/me/').view_name == 'api:me'
    catalog = reverse('web:catalog', kwargs={'organization_id': organization.pk})
    assert resolve(catalog).view_name == 'web:catalog'
    assert client.get(catalog).status_code == 200
    viewer = reverse('web:records', kwargs={'organization_id': organization.pk, 'table_key': 'example_notes.note'})
    assert viewer == f'/o/{organization.pk}/tables/example_notes.note/'
    assert resolve(viewer).view_name == 'web:records'
    assert not any(issubclass(model, SecuredRecord) for model in admin.site._registry)
    if apps.is_installed('examples.notes'):
        assert client.get(viewer).status_code == 200
    for path in ('/sk_accounts/', '/jsi18n/', '/not-a-registered-root/', '/admin/sk_accounts/'):
        with pytest.raises(Resolver404):
            resolve(path)
        assert client.get(path).status_code == 404


def test_record_routes_preserve_platform_permission_mfa_csrf_and_cas(platform_actor):
    user, _, client = platform_actor
    index = reverse('admin:records_index')
    change = reverse('admin:sk_accounts_user_change', args=[user.pk])
    assert Client().get(index).url.startswith(reverse('admin:login'))
    assert client.post(change, {'first_name': 'CSRF bypass'}).status_code == 403
    with override_settings(SKELETON_REQUIRE_ADMIN_MFA=True):
        for url in (index, change, reverse('admin:sk_accounts_user_changelist'), reverse('admin:index')):
            response = client.get(url)
            assert response.status_code == 302 and response.url == reverse('mfa_index')
    stale_revision = user.revision
    with system_context(reason='test:configuration-boundary-cas'):
        user.first_name = 'Concurrent winner'
        user.save(update_fields=['first_name'])
    assert client.post(change, {
        'email': user.email, 'first_name': 'Stale loser', 'last_name': '',
        'is_active': 'on', 'is_staff': 'on', 'is_superuser': 'on',
        'expected_revision': stale_revision, '_save': 'Save',
        'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
    }).status_code == 409
    assert User.objects.get(pk=user.pk).first_name == 'Concurrent winner'
    with system_context(reason='test:configuration-boundary-nonplatform'):
        user.refresh_from_db()
        user.is_staff = user.is_superuser = False
        user.save(update_fields=['is_staff', 'is_superuser'])
    nonplatform = login(user)
    assert nonplatform.get(index).status_code == 403
    assert nonplatform.get(change).status_code == 403
    assert nonplatform.get(reverse('admin:index')).status_code == 403
