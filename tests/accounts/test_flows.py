"""Consumer-visible identity transitions through real allauth views and mail links."""
import re
from urllib.parse import urlsplit

import pytest
from django.core import mail
from django.test import Client, RequestFactory, override_settings

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.tenancy.context import system_context, tenant_context
from skeleton.tenancy.invitations import issue_invitation

PASSWORD = 'Fixture-account-password-2026!493'
NEW_PASSWORD = 'Fixture-replacement-password-2026!821'


@pytest.fixture
def local_user(skeleton_db):
    from allauth.account.models import EmailAddress
    with system_context(reason='test:account-flow'):
        user = create_identity(email='flow-user@example.test', password=PASSWORD)
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def mail_path(fragment, email):
    messages = [message for message in mail.outbox if email in message.to and fragment in message.body]
    assert messages, 'The real mail backend must receive a materialized message.'
    links = re.findall(r'https?://[^\s<>\"]+', messages[-1].body)
    return urlsplit(next(link for link in links if fragment in link).rstrip('.,)')).path


def test_invitation_signup_uses_fixed_email_verified_proof_and_single_membership(skeleton_db):
    from allauth.account.models import EmailAddress
    from skeleton.accounts.models import User, UserProfile
    from skeleton.access.models import Role
    from skeleton.tenancy.models import Membership
    admin, organization = bootstrap_identity(email='invite-owner@example.test', organization='invite-flow', name='Invitation flow', password=PASSWORD)
    request = RequestFactory().get('/', HTTP_HOST='testserver')
    with tenant_context(organization_id=organization.pk, actor=admin, source='test:invite-flow'):
        invitation = issue_invitation(user=admin, email='invited-flow@example.test', role=Role.objects.get(code='member'), request=request)
    path = mail_path('/invite/', invitation.email)
    client = Client()
    assert client.get(path).status_code == 200
    reference = client.session['skeleton_invitation']
    assert set(reference) == {'organization', 'invitation', 'digest'}
    response = client.post('/accounts/signup/', {'email': 'mass-assignment@example.test', 'password1': PASSWORD, 'password2': PASSWORD})
    assert response.status_code == 302
    user = User.objects.get(email=invitation.email)
    assert not User.objects.filter(email='mass-assignment@example.test').exists()
    assert EmailAddress.objects.get(user=user, email=user.email).verified
    assert UserProfile.objects.filter(user=user).count() == 1
    assert client.get('/api/v1/me/').status_code == 200
    with tenant_context(organization_id=organization.pk, actor=user, source='test:invite-result'):
        assert Membership.objects.filter(user=user).count() == 1
    assert client.get(path).status_code == 400


def test_public_signup_requires_email_confirmation_and_has_no_organization(skeleton_db):
    from allauth.account.models import EmailAddress
    from skeleton.accounts.models import User
    from skeleton.tenancy.models import Membership
    client = Client()
    with override_settings(SKELETON_PUBLIC_SIGNUP=True):
        response = client.post('/accounts/signup/', {'email': 'public-flow@example.test', 'password1': PASSWORD, 'password2': PASSWORD})
    assert response.status_code == 302
    user = User.objects.get(email='public-flow@example.test')
    address = EmailAddress.objects.get(user=user)
    assert not address.verified
    assert client.get('/api/v1/me/').status_code == 403
    path = mail_path('/accounts/confirm-email/', user.email)
    assert client.get(path).status_code == 200
    assert client.post(path).status_code == 302
    address.refresh_from_db()
    assert address.verified
    assert not Membership.all_objects.filter(user=user).exists()


def reset_key_path(client, email):
    assert client.post('/accounts/password/reset/', {'email': email}).status_code == 302
    path = mail_path('/accounts/password/reset/key/', email)
    # allauth exchanges the key-bearing URL for a server-side session reference.
    response = client.get(path)
    return response['Location'] if response.status_code == 302 else path


def test_password_reset_real_mail_token_is_single_use_and_revokes_old_session(local_user):
    old = Client()
    assert old.post('/accounts/login/', {'login': local_user.email, 'password': PASSWORD}).status_code == 302
    reset = Client()
    path = reset_key_path(reset, local_user.email)
    assert reset.post(path, {'password1': NEW_PASSWORD, 'password2': NEW_PASSWORD}).status_code == 302
    local_user.refresh_from_db()
    assert local_user.check_password(NEW_PASSWORD)
    assert old.get('/api/v1/me/').status_code == 403
    assert reset.get('/api/v1/me/').status_code == 403  # reset is not a login bypass
    replay = reset.post(path, {'password1': PASSWORD, 'password2': PASSWORD})
    assert replay.status_code in {200, 302, 400}
    local_user.refresh_from_db()
    assert local_user.check_password(NEW_PASSWORD)


@pytest.mark.parametrize('state', ['sso_only', 'inactive', 'deleted'])
def test_issued_reset_token_cannot_create_password_after_policy_revocation(local_user, state):
    from skeleton.accounts.services import deactivate_user
    reset = Client()
    path = reset_key_path(reset, local_user.email)
    with system_context(reason='test:reset-policy'):
        if state == 'sso_only':
            local_user.local_password_allowed = False
            local_user.save(update_fields=['local_password_allowed'])
        else:
            deactivate_user(local_user, delete=state == 'deleted')
    response = reset.post(path, {'password1': NEW_PASSWORD, 'password2': NEW_PASSWORD})
    assert response.status_code in {200, 302, 400, 403}
    local_user.refresh_from_db()
    assert not local_user.check_password(NEW_PASSWORD)
    before = len(mail.outbox)
    assert Client().post('/accounts/password/reset/', {'email': local_user.email}).status_code == 302
    assert len(mail.outbox) == before


def test_admin_login_has_no_password_or_mfa_bypass(skeleton_db):
    admin, organization = bootstrap_identity(email='mfa-owner@example.test', organization='mfa-flow', name='MFA flow', password=PASSWORD)
    client = Client()
    response = client.post('/admin/login/', {'username': admin.email, 'password': PASSWORD})
    assert response.status_code == 302 and '/accounts/login/' in response['Location']
    assert client.get('/api/v1/me/').status_code == 403
    assert client.post('/accounts/login/', {'login': admin.email, 'password': PASSWORD}).status_code == 302
    with override_settings(SKELETON_REQUIRE_ADMIN_MFA=True):
        response = client.get(f'/api/v1/o/{organization.pk}/roles/')
        assert response.status_code == 403 and response.json()['code'] == 'mfa_required'
        assert client.get('/profile/').status_code == 200
        assert client.get('/accounts/2fa/').status_code == 200
        response = client.get('/admin/')
        assert response.status_code == 302 and '/accounts/2fa/' in response['Location']


def test_real_mfa_login_after_rotation_and_recovery_single_use(local_user):
    import time
    from cryptography.fernet import Fernet
    from django.core.management import call_command
    from allauth.mfa import app_settings
    from allauth.mfa.models import Authenticator
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
    from allauth.mfa.totp.internal.auth import TOTP, format_hotp_value, hotp_value, validate_totp_code
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    secret = 'JBSWY3DPEHPK3PXP'
    with override_settings(MFA_ENCRYPTION_KEYS=[old_key]), system_context(reason='test:mfa-enrollment'):
        TOTP.activate(local_user, secret)
        recovery = RecoveryCodes.activate(local_user)
        codes = recovery.generate_codes()
    with override_settings(MFA_ENCRYPTION_KEYS=[new_key, old_key]):
        call_command('rotate_mfa_keys')
    with override_settings(MFA_ENCRYPTION_KEYS=[new_key]):
        stored = Authenticator.objects.get(user=local_user, type=Authenticator.Type.TOTP)
        assert secret not in str(stored.data)
        client = Client()
        response = client.post('/accounts/login/', {'login': local_user.email, 'password': PASSWORD})
        assert response.status_code == 302 and '/accounts/2fa/' in response['Location']
        challenge = response['Location']
        assert client.get('/api/v1/me/').status_code == 403
        invalid = '000000'
        while validate_totp_code(secret, invalid):
            invalid = f'{(int(invalid) + 1) % 1000000:06d}'
        assert client.post(challenge, {'code': invalid}).status_code == 200
        assert client.get('/api/v1/me/').status_code == 403
        code = format_hotp_value(hotp_value(secret, int(time.time()) // app_settings.TOTP_PERIOD))
        assert client.post(challenge, {'code': code}).status_code == 302
        assert client.get('/api/v1/me/').status_code == 200
        assert client.post('/accounts/logout/').status_code == 302
        challenge = client.post('/accounts/login/', {'login': local_user.email, 'password': PASSWORD})['Location']
        assert client.post(challenge, {'code': codes[0]}).status_code == 302
        assert client.get('/api/v1/me/').status_code == 200
        assert client.post('/accounts/logout/').status_code == 302
        challenge = client.post('/accounts/login/', {'login': local_user.email, 'password': PASSWORD})['Location']
        assert client.post(challenge, {'code': codes[0]}).status_code == 200
        assert client.get('/api/v1/me/').status_code == 403


def test_platform_admin_lifecycle_conflicts_and_logical_delete(local_user):
    from django.urls import reverse
    from skeleton.accounts.models import User, UserProfile
    admin, _ = bootstrap_identity(email='lifecycle-owner@example.test', organization='admin-lifecycle',
                                  name='Admin lifecycle', password=PASSWORD)
    client = Client()
    assert client.post('/accounts/login/', {'login': admin.email, 'password': PASSWORD}).status_code == 302
    revision = client.get(reverse('admin:sk_accounts_user_change', args=[admin.pk])).context['adminform'].form['expected_revision'].value()
    changed = client.post(reverse('admin:sk_accounts_user_change', args=[admin.pk]), {
        'email': admin.email, 'first_name': '', 'last_name': '',
        'is_staff': 'on', 'is_superuser': 'on', '_save': 'Save',
        'expected_revision': revision,
    })
    assert changed.status_code == 409
    removed = client.post(reverse('admin:sk_accounts_user_delete', args=[admin.pk]), {'post': 'yes'})
    assert removed.status_code == 409
    admin.refresh_from_db()
    assert admin.is_active and admin.deleted_at is None
    profile_id = local_user.profile.pk
    removed = client.post(reverse('admin:sk_accounts_user_delete', args=[local_user.pk]), {'post': 'yes'})
    assert removed.status_code == 302
    retained = User.all_objects.get(pk=local_user.pk)
    assert not retained.is_active and retained.deleted_at is not None
    assert UserProfile.objects.get(pk=profile_id).user_id == retained.pk
    assert client.get('/api/v1/me/').status_code == 200
