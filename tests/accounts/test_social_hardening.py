"""Finding-specific regressions exercising allauth and configured provider boundaries."""
import json
from io import StringIO

import pytest
from allauth.core import context
from allauth.socialaccount.models import SocialApp, SocialToken
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import RequestFactory, override_settings

from skeleton.accounts.adapters import AccountAdapter, SocialAccountAdapter
from skeleton.accounts.providers import configured_providers, load_provider_config
from skeleton.accounts.services import bootstrap_identity
from skeleton.accounts.social_providers import MinimalOpenIDConnectProvider
from skeleton.tenancy.context import system_context, tenant_context


def entry():
    return {'protocol': 'oidc', 'provider_id': 'acme', 'organization_slug': 'acme',
            'jit_provisioning': True, 'trust_verified_email': True,
            'app': {'name': 'Acme', 'client_id': 'client-a', 'secret_env': 'HARDENING_SECRET',
                    'settings': {'server_url': 'https://idp.example.test/realms/acme'}}}


def load(tmp_path, monkeypatch, document=None):
    monkeypatch.setenv('HARDENING_SECRET', 'fixture-client-secret')
    path = tmp_path / 'providers.json'
    path.write_text(json.dumps({'providers': [document or entry()]}))
    return load_provider_config(path)


def request(query=''):
    req = RequestFactory().get('/accounts/oidc/acme/login/' + query)
    SessionMiddleware(lambda req: None).process_request(req)
    req.user = AnonymousUser()
    from django.contrib.messages.storage.fallback import FallbackStorage
    req._messages = FallbackStorage(req)
    return req


def provider(configuration, req):
    app = SocialApp(provider='openid_connect', **configuration.bindings[0].app)
    return MinimalOpenIDConnectProvider(req, app=app)


@pytest.mark.parametrize('fabricated', [True, ['example.test']])
def test_sp01_rejects_operator_fabricated_verified_email(tmp_path, monkeypatch, fabricated):
    document = entry()
    document['app']['settings']['verified_email'] = fabricated
    with pytest.raises(ImproperlyConfigured):
        load(tmp_path, monkeypatch, document)


@pytest.mark.parametrize('option,value', [('scope', ['openid', 'admin']), ('auth_params', {'redirect_uri': 'https://evil.example'})])
def test_sp02_rejects_configured_oauth_expansion(tmp_path, monkeypatch, option, value):
    document = entry()
    document['app']['settings'][option] = value
    with pytest.raises(ImproperlyConfigured):
        load(tmp_path, monkeypatch, document)


def test_sp02_request_cannot_expand_scope_or_override_protocol(tmp_path, monkeypatch):
    configuration = load(tmp_path, monkeypatch)
    req = request('?scope=admin,offline_access&auth_params=redirect_uri%3Dhttps%3A%2F%2Fevil.example%26client_id%3Devil')
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=configuration, SOCIALACCOUNT_PROVIDERS=configuration.settings):
        upstream = provider(configuration, req)
        assert upstream.get_scope_from_request(req) == ['openid', 'profile', 'email']
        assert upstream.get_auth_params_from_request(req, 'authenticate') == {}


def test_sp03_pending_signup_serializes_no_tokens_or_protocol_secrets(skeleton_db, tmp_path, monkeypatch):
    from allauth.socialaccount.internal.flows.signup import get_pending_signup, redirect_to_signup
    configuration = load(tmp_path, monkeypatch)
    bootstrap_identity(email='owner@example.test', organization='acme', name='Acme', password='Fixture-password-2026!')
    req = request()
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=configuration, SOCIALACCOUNT_PROVIDERS=configuration.settings), context.request_context(req):
        login = provider(configuration, req).sociallogin_from_response(req, {'sub': 'pending-subject', 'email': 'pending@example.test', 'email_verified': False})
        login.token = SocialToken(token='fixture-access-token', token_secret='fixture-refresh-token')
        login.state = {'process': 'login', 'pkce_code_verifier': 'fixture-pkce-secret', 'next': '/profile/'}
        SocialAccountAdapter().pre_social_login(req, login)
        redirect_to_signup(req, login)
        serialized = json.dumps(req.session['socialaccount_sociallogin'])
        for secret in ('fixture-access-token', 'fixture-refresh-token', 'fixture-pkce-secret', 'fixture-client-secret'):
            assert secret not in serialized
        restored = get_pending_signup(req)
        assert restored.account.uid == 'pending-subject'
        assert restored.state['process'] == 'login'
        assert restored.state['next'] == '/profile/'


@pytest.mark.parametrize('protocol', ['oidc', 'saml', 'github'])
def test_sp04_client_identity_changes_fingerprint(tmp_path, monkeypatch, protocol):
    document = entry()
    document['protocol'] = protocol
    if protocol == 'github':
        document['provider_id'] = 'github'
        document['app']['settings'] = {}
    elif protocol == 'saml':
        document['app']['settings'] = {'idp': {'entity_id': 'https://idp.example.test', 'x509cert': 'public-certificate'}}
    first = load(tmp_path, monkeypatch, document)
    document['app']['client_id'] = 'client-b'
    second = load(tmp_path, monkeypatch, document)
    assert first.bindings[0].fingerprint != second.bindings[0].fingerprint


def test_sp05_binding_preflight_command_checks_runtime_organization(skeleton_db, tmp_path, monkeypatch):
    configuration = load(tmp_path, monkeypatch)
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=configuration):
        with pytest.raises(ImproperlyConfigured, match='active organization'):
            call_command('validate_provider_bindings', database='default', stdout=StringIO())


def test_sbr10_missing_startup_snapshot_never_rereads_provider_file(tmp_path, monkeypatch):
    load(tmp_path, monkeypatch)
    monkeypatch.setenv('SSO_PROVIDER_CONFIG_PATH', str(tmp_path / 'providers.json'))
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=None):
        with pytest.raises(ImproperlyConfigured):
            configured_providers()


@pytest.mark.parametrize('nested', [
    {'sp': {'private_key': 'literal-key'}},
    {'nested': [{'secret': 'literal-secret'}]},
    {'sp': {'private_key': 'literal-key', 'private_key_env': 'HARDENING_SECRET'}},
    {'sp': {'private_key_env': 'HARDENING_SECRET', 'private_key': 'literal-key'}},
])
def test_sbr13_nested_literal_secrets_and_ambiguous_references_rejected(tmp_path, monkeypatch, nested):
    document = entry()
    document['app']['settings'].update(nested)
    with pytest.raises(ImproperlyConfigured):
        load(tmp_path, monkeypatch, document)


def test_sbr03_confirming_additional_email_does_not_reactivate_suspended_jit(skeleton_db, tmp_path, monkeypatch):
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount
    from skeleton.access.models import MemberRole
    from skeleton.access.services import update_membership
    from skeleton.accounts.adapters import provision_social_membership
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.models import Membership
    configuration = load(tmp_path, monkeypatch)
    admin, organization = bootstrap_identity(email='owner@example.test', organization='acme', name='Acme', password='Fixture-password-2026!')
    with system_context(reason='test:social-admission'):
        user = create_identity(email='jit@example.test', password=None)
        extra_email = EmailAddress.objects.create(user=user, email='additional@example.test', verified=False)
        SocialAccount.objects.create(user=user, provider='acme', uid='jit-subject', extra_data={'skeleton_binding': configuration.bindings[0].fingerprint})
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=configuration):
        provision_social_membership(user, configuration.bindings[0])
        with tenant_context(organization_id=organization.pk, actor=admin, source='test:suspend-jit'):
            membership = Membership.objects.get(user=user)
            update_membership(membership, user=admin, data={'is_active': False})
        req = request()
        with context.request_context(req):
            assert AccountAdapter().confirm_email(req, extra_email)
        with tenant_context(organization_id=organization.pk, actor=admin, source='test:confirm-jit'):
            membership.refresh_from_db()
            assert not membership.is_active
            assert not MemberRole.objects.filter(membership=membership).exists()


def test_sbr03_retry_before_confirmation_preserves_initial_admission(skeleton_db, tmp_path, monkeypatch):
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.models import Membership
    configuration = load(tmp_path, monkeypatch)
    admin, organization = bootstrap_identity(email='owner@example.test', organization='acme', name='Acme', password='Fixture-password-2026!')
    with system_context(reason='test:pending-social-admission'):
        user = create_identity(email='pending-retry@example.test', password=None)
        address = EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=False)
        account = SocialAccount.objects.create(user=user, provider='acme', uid='pending-retry-subject', extra_data={
            'skeleton_binding': configuration.bindings[0].fingerprint,
            'skeleton_pending_email': user.email,
        })
    req = request()
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=configuration, SOCIALACCOUNT_PROVIDERS=configuration.settings), context.request_context(req):
        sociallogin = provider(configuration, req).sociallogin_from_response(req, {
            'sub': account.uid, 'email': user.email, 'email_verified': False,
        })
        sociallogin.lookup()
        SocialAccountAdapter().pre_social_login(req, sociallogin)
        account.refresh_from_db()
        assert account.extra_data.get('skeleton_pending_email') == user.email
        assert AccountAdapter().confirm_email(req, address)
    with tenant_context(organization_id=organization.pk, actor=admin, source='test:pending-admission-observe'):
        assert Membership.objects.get(user=user).is_active
    account.refresh_from_db()
    assert 'skeleton_pending_email' not in account.extra_data


@pytest.mark.parametrize('claim,verified', [(True, True), ('true', False), ('false', False), (1, False), (['true'], False), (None, False)])
@pytest.mark.parametrize('source', ['id_token', 'userinfo'])
def test_oidc_email_proof_uses_strict_boolean_from_the_selected_claims(skeleton_db, tmp_path, monkeypatch, claim, verified, source):
    configuration = load(tmp_path, monkeypatch)
    req = request()
    claims = {'sub': 'strict-proof-subject', 'email': 'proof@example.test', 'email_verified': claim}
    response = {source: claims}
    if source == 'userinfo':
        response['id_token'] = {'sub': 'strict-proof-subject', 'email': 'another@example.test', 'email_verified': True}
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=configuration, SOCIALACCOUNT_PROVIDERS=configuration.settings), context.request_context(req):
        login = provider(configuration, req).sociallogin_from_response(req, response)
    assert login.email_addresses[0].email == 'proof@example.test'
    assert login.email_addresses[0].verified is verified
