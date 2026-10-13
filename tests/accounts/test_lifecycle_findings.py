import json
import pytest
from allauth.account.models import EmailAddress
from django.test import Client
from django.urls import reverse

from skeleton.accounts.models import User, UserProfile
from skeleton.accounts.services import bootstrap_identity, create_identity, deactivate_user, restore_user
from skeleton.tenancy.context import system_context

PASSWORD = 'Lifecycle-finding-password!93'
pytestmark = pytest.mark.usefixtures('skeleton_db')


def identity(email='lifecycle@example.test'):
    with system_context(reason='test:lifecycle-identity'):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def login(user, **kwargs):
    client = Client(**kwargs)
    assert client.post('/accounts/login/', {'login': user.email, 'password': PASSWORD}).status_code == 302
    return client


def test_rd01_primary_email_change_revokes_other_session_but_preserves_current():
    user = identity()
    with system_context(reason='test:additional-email'):
        EmailAddress.objects.create(user=user, email='new-primary@example.test', verified=True)
    first, second = login(user), login(user)
    before = User.objects.get(pk=user.pk).auth_version
    response = first.post(reverse('account_email'), {'email': 'new-primary@example.test', 'action_primary': ''})
    assert response.status_code == 302
    user.refresh_from_db()
    assert user.email == 'new-primary@example.test'
    assert user.auth_version == before + 1
    assert first.get('/api/v1/me/').status_code == 200
    assert second.get('/api/v1/me/').status_code == 403


def test_rd06_restore_user_persists_version_with_one_cas():
    user = identity()
    from uuid import uuid4
    from skeleton.tenancy.context import request_context
    admin, _ = bootstrap_identity(email='restore-platform@example.test', organization='restore-platform', name='Restore platform', password=PASSWORD)
    with request_context(actor=admin, request_id=uuid4(), source='test:restore-user'):
        user = deactivate_user(user, delete=True)
        revision, version = user.revision, user.auth_version
        restored = restore_user(user)
        persisted = User.all_objects.get(pk=user.pk)
        assert persisted.pk == restored.pk
        assert not persisted.is_active and persisted.deleted_at is None
        assert persisted.auth_version == version + 1
        assert persisted.revision == revision + 1
        assert restored.auth_version == persisted.auth_version


def test_sbr02_admin_deactivation_preserves_form_changes_and_revokes_sessions():
    admin, _ = bootstrap_identity(email='platform-owner@example.test', organization='platform', name='Platform', password=PASSWORD)
    user = identity('offboarding@example.test')
    old_session = login(user)
    platform = login(admin, raise_request_exception=False)
    user.refresh_from_db()  # allauth login persisted last_login before the form was opened.
    response = platform.post(reverse('admin:sk_accounts_user_change', args=[user.pk]), {
        'email': user.email, 'first_name': 'Saved during offboarding', 'last_name': '',
        'expected_revision': user.revision, '_save': 'Save',
    })
    assert response.status_code == 302
    user.refresh_from_db()
    assert not user.is_active
    assert user.first_name == 'Saved during offboarding'
    assert old_session.get('/api/v1/me/').status_code == 403


def test_sbr02_admin_stale_form_conflicts_without_losing_newer_changes():
    admin, _ = bootstrap_identity(email='platform-owner@example.test', organization='platform', name='Platform', password=PASSWORD)
    user = identity('stale-offboarding@example.test')
    revision = user.revision
    with system_context(reason='test:concurrent-platform-write'):
        user.first_name = 'Concurrent winner'
        user.save(update_fields=['first_name'])
    platform = login(admin, raise_request_exception=False)
    response = platform.post(reverse('admin:sk_accounts_user_change', args=[user.pk]), {
        'email': user.email, 'first_name': 'Stale loser', 'last_name': '',
        'expected_revision': revision, '_save': 'Save',
    })
    assert response.status_code == 409
    user.refresh_from_db()
    assert user.is_active and user.first_name == 'Concurrent winner'


@pytest.mark.parametrize('value', [[], {}])
def test_sbr11_non_string_language_is_validation_error_without_write(value):
    user = identity()
    client = login(user, raise_request_exception=False)
    before = client.get('/api/v1/me/')
    response = client.patch('/api/v1/me/', json.dumps({'first_name': 'Must rollback', 'language': value}),
                            content_type='application/json', HTTP_IF_MATCH=before['ETag'])
    assert response.status_code == 400
    assert response.json()['code'] == 'validation_error'
    assert client.get('/api/v1/me/').json() == before.json()


@pytest.mark.parametrize('method', ['create_user', 'create_superuser'])
def test_sbr12_manager_created_identity_has_persisted_profile_and_me(method):
    with system_context(reason='test:manager-identity'):
        user = getattr(User.objects, method)(email=f'{method}@example.test', password=PASSWORD)
        assert UserProfile.objects.get(user=user).timezone == 'UTC'
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    response = login(user).get('/api/v1/me/')
    assert response.status_code == 200
    assert response.json()['profile']['sysid'] == str(user.profile.pk)
