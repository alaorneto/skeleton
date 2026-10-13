from datetime import timedelta
from hashlib import sha256
import secrets

from cryptography.fernet import Fernet, MultiFernet
from django.contrib.auth import authenticate
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone
import pytest

from skeleton.accounts.adapters import AccountAdapter, EncryptedMFAAdapter
from skeleton.accounts.forms import PolicyResetPasswordKeyForm
from skeleton.accounts.services import bootstrap_identity, create_identity, set_local_password, update_profile
from skeleton.tenancy.context import system_context, tenant_context
from skeleton.tenancy.invitations import InvalidInvitation, accept_invitation, pending_invitation, validate_invitation

PASSWORD = 'Discardable-Identity-Test-Password!493'


@pytest.fixture
def identity(skeleton_db):
    with system_context(reason='test:identity'):
        user = create_identity(email='alice@example.test', password=PASSWORD)
        from allauth.account.models import EmailAddress
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def test_password_backend_rejects_sso_only(identity):
    assert authenticate(email=identity.email, password=PASSWORD) == identity
    with system_context(reason='test:sso-only'):
        identity.local_password_allowed = False
        identity.save(update_fields=['local_password_allowed'])
    assert authenticate(email=identity.email, password=PASSWORD) is None
    with pytest.raises(PermissionDenied), system_context(reason='test:password'):
        set_local_password(identity, PASSWORD)
    assert not PolicyResetPasswordKeyForm(user=identity, data={'password1': PASSWORD, 'password2': PASSWORD}).is_valid()


def test_auth_version_invalidates_real_login_session(identity):
    client = Client()
    response = client.post('/accounts/login/', {'login': identity.email, 'password': PASSWORD})
    assert response.status_code == 302
    assert client.session['skeleton_auth_version'] == identity.auth_version
    from skeleton.accounts.services import revoke_sessions
    with system_context(reason='test:revoke'):
        revoke_sessions(identity)
    assert client.get('/api/v1/me/').status_code == 403


def test_closed_signup_does_not_create_identity(skeleton_db):
    from skeleton.accounts.models import User
    with override_settings(SKELETON_PUBLIC_SIGNUP=False):
        response = Client().post('/accounts/signup/', {'email': 'closed@example.test', 'password1': PASSWORD, 'password2': PASSWORD})
    assert response.status_code in {200, 302, 403}
    assert not User.objects.filter(email='closed@example.test').exists()


def test_profile_rejects_flags_and_checks_both_revisions(identity):
    from skeleton.common.exceptions import ConcurrentUpdate
    profile = identity.profile
    with system_context(reason='test:profile'):
        with pytest.raises(ValidationError):
            update_profile(identity, data={'is_superuser': True})
        changed, changed_profile = update_profile(identity, data={'first_name': 'Alice', 'display_name': 'A'}, user_revision=identity.revision, profile_revision=profile.revision)
        assert changed.first_name == 'Alice'
        with pytest.raises(ConcurrentUpdate):
            update_profile(changed, data={'last_name': 'Stale'}, user_revision=identity.revision, profile_revision=changed_profile.revision)


def test_bootstrap_idempotency(skeleton_db):
    first, org = bootstrap_identity(email='admin@example.test', organization='test', name='Test', password=PASSWORD)
    encoded = first.password
    second, same_org = bootstrap_identity(email='admin@example.test', organization='test', name='Ignored', password='NeverReplacePassword!23')
    assert first.pk == second.pk and org.pk == same_org.pk
    assert second.password == encoded and same_org.name == 'Test'


def test_invitation_single_use_email_bound_and_no_plaintext_session(skeleton_db, identity):
    admin, org = bootstrap_identity(email='admin@example.test', organization='invites', name='Invites', password=PASSWORD)
    from skeleton.tenancy.models import Invitation
    from skeleton.access.models import Role
    token = secrets.token_urlsafe(32)
    with system_context(reason='test:invitation', organization_id=org.pk):
        role = Role.objects.get(code='member')
        invite = Invitation(organization=org, email=identity.email, role=role, token_digest=sha256(token.encode()).hexdigest(), expires_at=timezone.now() + timedelta(days=7))
        invite.save()
    proof = validate_invitation(org.pk, token)
    request = RequestFactory().get('/')
    request.session = {'skeleton_invitation': {'organization': str(org.pk), 'invitation': str(proof.sysid), 'digest': proof.token_digest}}
    assert token not in str(request.session)
    assert pending_invitation(request).email == identity.email
    with pytest.raises(InvalidInvitation):
        accept_invitation(user=admin, organization_id=org.pk, token=token)
    membership = accept_invitation(user=identity, organization_id=org.pk, token=token)
    assert membership.user_id == identity.pk
    with pytest.raises(InvalidInvitation):
        accept_invitation(user=identity, organization_id=org.pk, token=token)


def test_expired_invitation_rejects_previously_valid_session_proof(identity, monkeypatch):
    from skeleton.tenancy.models import Invitation, Membership
    from skeleton.access.models import Role
    _, organization = bootstrap_identity(email='expiry-owner@example.test', organization='expired-invites', name='Expiry', password=PASSWORD)
    token = secrets.token_urlsafe(32)
    with system_context(reason='test:invitation-expiry', organization_id=organization.pk):
        invitation = Invitation(organization=organization, email=identity.email,
                                role=Role.objects.get(code='member'),
                                token_digest=sha256(token.encode()).hexdigest(),
                                expires_at=timezone.now() + timedelta(days=7))
        invitation.save()
    proof = validate_invitation(organization.pk, token)
    request = RequestFactory().get('/')
    request.session = {'skeleton_invitation': {'organization': str(organization.pk), 'invitation': str(proof.sysid), 'digest': proof.token_digest}}
    # Expiry is immutable; advance the clock exactly to the exclusive deadline.
    monkeypatch.setattr(timezone, 'now', lambda: invitation.expires_at)
    assert pending_invitation(request) is None
    assert 'skeleton_invitation' not in request.session
    with pytest.raises(InvalidInvitation):
        accept_invitation(user=identity, organization_id=organization.pk, proof=proof)
    with system_context(reason='test:invitation-expiry', organization_id=organization.pk):
        assert not Membership.objects.filter(user=identity).exists()


def test_mfa_cipher_and_rotation_preserve_consumed_codes(identity):
    from allauth.mfa.models import Authenticator
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
    from allauth.mfa.totp.internal.auth import TOTP
    old, new = Fernet.generate_key(), Fernet.generate_key()
    with override_settings(MFA_ENCRYPTION_KEYS=[old]):
        with system_context(reason='test:mfa'):
            totp = TOTP.activate(identity, 'JBSWY3DPEHPK3PXP')
            recovery = RecoveryCodes.activate(identity)
            codes = recovery.generate_codes()
            assert recovery.validate_code(codes[0])
    with override_settings(MFA_ENCRYPTION_KEYS=[new, old]):
        call_command('rotate_mfa_keys')
    with override_settings(MFA_ENCRYPTION_KEYS=[new]), system_context(reason='test:mfa-read'):
        totp.instance.refresh_from_db()
        recovery.instance.refresh_from_db()
        assert EncryptedMFAAdapter().decrypt(totp.instance.data['secret']) == 'JBSWY3DPEHPK3PXP'
        assert not recovery.validate_code(codes[0])
        assert recovery.validate_code(codes[1])
        assert 'JBSWY3DPEHPK3PXP' not in str(totp.instance.data)
