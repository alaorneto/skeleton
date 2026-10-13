"""Gov.br profile contracts through allauth, with real signed local JWT fixtures.

The bounded HTTP fixture is not government homologation or a credentialed smoke.
"""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import jwt
import pytest
import requests
from allauth.core import context
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings

from skeleton.accounts.adapters import SocialAccountAdapter
from skeleton.accounts.providers import GOVBR_ISSUERS, load_provider_config
from skeleton.accounts.social_providers import MinimalOpenIDConnectProvider
from skeleton.tenancy.context import request_context, system_context, tenant_context
from tests.accounts.test_flows import PASSWORD, mail_path
from tests.accounts.test_social_hardening import request


CALLBACK = '/accounts/oidc/govbr-staging/login/callback/'
LOGIN = '/accounts/oidc/govbr-staging/login/'
LOGOUT_CALLBACK = 'https://service.gov.br/accounts/login/'


def entry():
    return {'protocol': 'govbr', 'provider_id': 'govbr-staging',
            'environment': 'staging', 'post_logout_redirect_uri': LOGOUT_CALLBACK,
            'trust_verified_email': True,
            'app': {'client_id': 'fixture.gov.br', 'secret_env': 'GOVBR_TEST_SECRET'}}


def load(tmp_path, monkeypatch, document=None):
    monkeypatch.setenv('GOVBR_TEST_SECRET', 'fixture-only-secret')
    path = tmp_path / 'govbr.json'
    path.write_text(json.dumps({'providers': [document or entry()]}))
    return load_provider_config(path)


class GovernmentHTTPFixture:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        public.update(kid='fixture-key', alg='RS256', use='sig')
        self.jwks = {'keys': [public]}
        self.claims = {}
        self.signing_key = self.key
        self.token_requests = []
        self.get_requests = []
        self.include_id_token = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @staticmethod
    def response(data):
        response = requests.Response()
        response.status_code = 200
        response.headers['content-type'] = 'application/json'
        response._content = json.dumps(data).encode()
        return response

    def request(self, method, url, **kwargs):
        assert method == 'POST'
        assert url == GOVBR_ISSUERS['staging'] + 'token'
        self.token_requests.append(kwargs)
        response = {'access_token': 'fixture-access-token', 'token_type': 'Bearer', 'expires_in': 60}
        if self.include_id_token:
            response['id_token'] = jwt.encode(self.claims, self.signing_key, algorithm='RS256', headers={'kid': 'fixture-key'})
        return self.response(response)

    def get(self, url, **kwargs):
        self.get_requests.append(url)
        assert url == GOVBR_ISSUERS['staging'] + 'jwk'
        return self.response(self.jwks)

    def prepare(self, authorization, **overrides):
        now = int(time.time())
        self.claims = {'iss': GOVBR_ISSUERS['staging'], 'aud': 'fixture.gov.br',
                       'sub': '00000000000', 'iat': now, 'exp': now + 60,
                       'jti': str(uuid4()), 'nonce': authorization['nonce'][0],
                       'email': 'citizen@example.test', 'email_verified': 'true',
                       'preferred_username': '00000000000', 'phone_number': '+550000000',
                       'picture': 'https://private.example.test/photo',
                       'reliability_info': {'level': 'gold'}, 'is_superuser': True,
                       'organization_slug': 'untrusted', 'amr': ['mfa']}
        self.claims.update(overrides)


@pytest.fixture
def govbr(skeleton_db, tmp_path, monkeypatch):
    configuration = load(tmp_path, monkeypatch)
    transport = GovernmentHTTPFixture()
    monkeypatch.setattr(SocialAccountAdapter, 'get_requests_session', lambda self: transport)
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=configuration,
                           SOCIALACCOUNT_PROVIDERS=configuration.settings):
        yield configuration, transport


def start(client, **query):
    response = client.post(LOGIN, query)
    assert response.status_code == 302
    assert urlsplit(response['Location']).netloc == 'sso.staging.acesso.gov.br'
    return parse_qs(urlsplit(response['Location']).query)


def finish(client, authorization):
    return client.get(CALLBACK, {'code': 'fixture-authorization-code', 'state': authorization['state'][0]})


def test_global_profile_endpoints_and_identity_domains(tmp_path, monkeypatch):
    staging = load(tmp_path, monkeypatch)
    document = entry()
    document['environment'] = 'production'
    production = load(tmp_path, monkeypatch, document)
    assert staging.bindings[0].organization_slug is None
    assert not staging.bindings[0].jit_provisioning
    assert staging.bindings[0].fingerprint != production.bindings[0].fingerprint
    assert staging.bindings[0].app['settings']['fetch_userinfo'] is False
    assert production.bindings[0].app['settings']['server_url'] == GOVBR_ISSUERS['production']


@pytest.mark.parametrize('field,value', [
    ('organization_slug', 'untrusted'), ('jit_provisioning', True),
    ('default_role_code', 'organization_admin'), ('environment', 'arbitrary'),
    ('post_logout_redirect_uri', 'http://service.gov.br/logout/'),
    ('post_logout_redirect_uri', 'https://service.gov.br/logout/?next=https://evil.test'),
])
def test_global_profile_rejects_tenant_and_redirect_overrides(tmp_path, monkeypatch, field, value):
    document = entry()
    document[field] = value
    with pytest.raises(ImproperlyConfigured):
        load(tmp_path, monkeypatch, document)


@pytest.mark.parametrize('claim,verified', [(True, True), ('true', True), (False, False),
                                         ('false', False), ('TRUE', False), (1, False),
                                         (None, False), (['true'], False)])
def test_verified_email_is_strict_and_claims_are_minimal(govbr, claim, verified):
    configuration, _ = govbr
    req = request()
    app = SocialApp(provider='openid_connect', **configuration.bindings[0].app)
    with context.request_context(req):
        provider = MinimalOpenIDConnectProvider(req, app=app)
        sociallogin = provider.sociallogin_from_response(req, {'id_token': {
            'sub': '00000000000', 'email': 'citizen@example.test', 'email_verified': claim,
            'preferred_username': '00000000000', 'phone_number': 'private',
            'picture': 'private', 'name': 'private', 'reliability_info': {'level': 'gold'}}})
    assert sociallogin.email_addresses[0].verified is verified
    assert sociallogin.account.extra_data == {'skeleton_binding': configuration.bindings[0].fingerprint}
    assert sociallogin.user.first_name == sociallogin.user.last_name == ''
    assert str(provider.wrap_account(sociallogin.account)) == 'GOV.BR'
    assert provider.supports_token_authentication is False


def test_authorization_is_post_csrf_and_has_fixed_scope_nonce_and_pkce(govbr):
    _, transport = govbr
    client = Client(enforce_csrf_checks=True)
    assert client.get(LOGIN).status_code == 200  # confirmation, not an IdP redirect
    assert client.post(LOGIN).status_code == 403
    client.get('/accounts/login/')
    csrf = client.cookies['csrftoken'].value
    response = client.post(LOGIN + '?scope=admin&auth_params=nonce%3Devil', HTTP_X_CSRFTOKEN=csrf)
    authorization = parse_qs(urlsplit(response['Location']).query)
    assert set(authorization['scope'][0].split()) == {'openid', 'profile', 'email'}
    assert authorization['response_type'] == ['code']
    assert authorization['code_challenge_method'] == ['S256']
    assert authorization['nonce'][0] != 'evil'
    assert len(authorization['nonce'][0]) >= 32
    transport.prepare(authorization)
    with override_settings(SKELETON_PUBLIC_SIGNUP=True):
        assert finish(client, authorization).status_code == 302
    exchange = transport.token_requests[0]
    assert exchange['auth'].username == 'fixture.gov.br'
    assert exchange['auth'].password == 'fixture-only-secret'
    assert 'client_secret' not in exchange['data']
    verifier = exchange['data']['code_verifier']
    assert 43 <= len(verifier) <= 128
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    assert authorization['code_challenge'] == [challenge]
    assert transport.get_requests == [GOVBR_ISSUERS['staging'] + 'jwk']


@pytest.mark.parametrize('invalid', ['signature', 'issuer', 'audience', 'expiry', 'nonce', 'missing_nonce', 'missing_id_token'])
def test_invalid_id_tokens_fail_before_identity_writes(govbr, invalid):
    from skeleton.accounts.models import User
    _, transport = govbr
    client = Client()
    authorization = start(client)
    transport.prepare(authorization)
    if invalid == 'signature':
        transport.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    elif invalid == 'issuer':
        transport.claims['iss'] = GOVBR_ISSUERS['production']
    elif invalid == 'audience':
        transport.claims['aud'] = 'other-client'
    elif invalid == 'expiry':
        transport.claims['exp'] = int(time.time()) - 60
    elif invalid == 'nonce':
        transport.claims['nonce'] = 'unbound-nonce'
    elif invalid == 'missing_nonce':
        transport.claims.pop('nonce')
    else:
        transport.include_id_token = False
    with override_settings(SKELETON_PUBLIC_SIGNUP=True):
        finish(client, authorization)
    assert not User.objects.exists()
    assert not SocialAccount.objects.exists()
    assert client.get('/api/v1/me/').status_code == 403


def test_state_is_one_use_and_missing_state_never_exchanges_code(govbr):
    _, transport = govbr
    client = Client()
    authorization = start(client)
    transport.prepare(authorization)
    client.get(CALLBACK, {'code': 'fixture-code', 'state': 'unknown-state'})
    assert not transport.token_requests
    with override_settings(SKELETON_PUBLIC_SIGNUP=True):
        assert finish(client, authorization).status_code == 302
    finish(client, authorization)
    assert len(transport.token_requests) == 1


def test_public_identity_has_no_affiliation_privilege_claims_or_token_session(govbr):
    from allauth.account.models import EmailAddress
    from skeleton.accounts.models import User, UserProfile
    from skeleton.tenancy.models import Membership
    configuration, transport = govbr
    client = Client()
    authorization = start(client)
    transport.prepare(authorization)
    with override_settings(SKELETON_PUBLIC_SIGNUP=True):
        response = finish(client, authorization)
    assert response.status_code == 302 and 'code=' not in response['Location']
    user = User.objects.get(email='citizen@example.test')
    assert not user.is_staff and not user.is_superuser and not user.local_password_allowed
    assert UserProfile.objects.filter(user=user).count() == 1
    assert EmailAddress.objects.get(user=user).verified
    assert not Membership.all_objects.filter(user=user).exists()
    assert SocialAccount.objects.get(user=user).extra_data == {'skeleton_binding': configuration.bindings[0].fingerprint}
    session = json.dumps(dict(client.session))
    assert 'fixture-access-token' not in session and 'reliability_info' not in session
    assert 'phone_number' not in session and 'pkce_code_verifier' not in session
    assert client.get('/api/v1/me/').status_code == 200
    assert client.get('/api/v1/organizations/').json() == {'results': []}
    connections = client.get('/accounts/3rdparty/')
    assert connections.status_code == 200 and b'00000000000' not in connections.content


def test_closed_admission_rejects_first_identity(govbr):
    from skeleton.accounts.models import User
    _, transport = govbr
    client = Client()
    authorization = start(client)
    transport.prepare(authorization)
    finish(client, authorization)
    assert not User.objects.exists()
    assert not SocialAccount.objects.exists()


def test_missing_email_uses_allauth_collection_and_local_confirmation(govbr):
    from allauth.account.models import EmailAddress
    from skeleton.accounts.models import User
    from skeleton.tenancy.models import Membership
    _, transport = govbr
    client = Client()
    authorization = start(client)
    transport.prepare(authorization, email=None, email_verified=False)
    with override_settings(SKELETON_PUBLIC_SIGNUP=True):
        response = finish(client, authorization)
        assert response.status_code == 302 and '/accounts/3rdparty/signup/' in response['Location']
        pending = json.dumps(dict(client.session))
        assert 'fixture-access-token' not in pending and 'phone_number' not in pending
        assert not User.objects.exists()
        response = client.post(response['Location'], {'email': 'collected@example.test'})
        assert response.status_code == 302
    user = User.objects.get(email='collected@example.test')
    address = EmailAddress.objects.get(user=user)
    assert not address.verified
    assert not Membership.all_objects.filter(user=user).exists()
    assert client.get('/api/v1/me/').status_code == 403
    path = mail_path('/accounts/confirm-email/', user.email)
    assert client.post(path).status_code == 302
    address.refresh_from_db()
    assert address.verified
    assert not Membership.all_objects.filter(user=user).exists()


def test_invitation_allows_missing_email_without_claim_affiliation(govbr):
    from skeleton.access.models import Role
    from skeleton.accounts.models import User
    from skeleton.accounts.services import bootstrap_identity
    from skeleton.tenancy.invitations import issue_invitation
    from skeleton.tenancy.models import Membership
    from django.test import RequestFactory
    _, transport = govbr
    admin, organization = bootstrap_identity(email='owner@example.test', organization='invited-govbr', name='Invited', password=PASSWORD)
    with tenant_context(organization_id=organization.pk, actor=admin, source='test:govbr-invite'):
        invitation = issue_invitation(user=admin, email='invited@example.test', role=Role.objects.get(code='member'), request=RequestFactory().get('/'))
    client = Client()
    client.get(mail_path('/invite/', invitation.email))
    authorization = start(client)
    transport.prepare(authorization, email=None, email_verified=False)
    response = finish(client, authorization)
    response = client.post(response['Location'], {'email': 'attacker@example.test'})
    assert response.status_code == 302
    user = User.objects.get(email=invitation.email)
    assert not User.objects.filter(email='attacker@example.test').exists()
    with tenant_context(organization_id=organization.pk, actor=user, source='test:govbr-result'):
        assert Membership.objects.filter(user=user).count() == 1


def test_email_collision_requires_explicit_recently_authenticated_connection(govbr):
    from allauth.account.models import EmailAddress
    from skeleton.accounts.services import create_identity
    _, transport = govbr
    with system_context(reason='test:govbr-existing'):
        user = create_identity(email='citizen@example.test', password=PASSWORD)
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    anonymous = Client()
    authorization = start(anonymous)
    transport.prepare(authorization)
    assert finish(anonymous, authorization)['Location'] == '/accounts/3rdparty/login/error/'
    assert not SocialAccount.objects.exists()
    client = Client()
    assert client.post('/accounts/login/', {'login': user.email, 'password': PASSWORD}).status_code == 302
    session = client.session
    session['account_authentication_methods'][-1]['at'] = 0
    session.save()
    authorization = start(client, process='connect')
    transport.prepare(authorization, email=None)
    assert finish(client, authorization)['Location'] == '/accounts/3rdparty/login/error/'
    assert not SocialAccount.objects.exists()
    assert client.post('/accounts/reauthenticate/', {'password': PASSWORD}).status_code == 302
    authorization = start(client, process='connect')
    transport.prepare(authorization, email=None)
    assert finish(client, authorization).status_code == 302
    assert SocialAccount.objects.get().user_id == user.pk
    user.refresh_from_db()
    assert user.local_password_allowed and user.check_password(PASSWORD)


def test_logout_is_local_post_then_allowlisted_browser_redirect(govbr):
    _, transport = govbr
    client = Client(enforce_csrf_checks=True)
    client.get('/accounts/login/')
    csrf = client.cookies['csrftoken'].value
    response = client.post(LOGIN, HTTP_X_CSRFTOKEN=csrf)
    authorization = parse_qs(urlsplit(response['Location']).query)
    transport.prepare(authorization)
    with override_settings(SKELETON_PUBLIC_SIGNUP=True):
        assert finish(client, authorization).status_code == 302
    assert client.get('/accounts/logout/').status_code == 200
    assert client.get('/api/v1/me/').status_code == 200
    assert client.post('/accounts/logout/').status_code == 403
    csrf = client.cookies['csrftoken'].value
    response = client.post('/accounts/logout/?next=https://evil.example.test', HTTP_X_CSRFTOKEN=csrf)
    target = urlsplit(response['Location'])
    assert target.netloc == 'sso.staging.acesso.gov.br' and target.path == '/logout'
    assert parse_qs(target.query) == {'post_logout_redirect_uri': [LOGOUT_CALLBACK]}
    assert client.get('/api/v1/me/').status_code == 403


@pytest.mark.parametrize('claim', ['exp', 'iat'])
def test_missing_required_time_claims_are_rejected(govbr, claim):
    from skeleton.accounts.models import User
    _, transport = govbr
    client = Client()
    authorization = start(client)
    transport.prepare(authorization)
    transport.claims.pop(claim)
    with override_settings(SKELETON_PUBLIC_SIGNUP=True):
        finish(client, authorization)
    assert not User.objects.exists()
    assert not SocialAccount.objects.exists()


def test_provider_state_cannot_be_consumed_by_another_callback(govbr):
    from dataclasses import replace
    from copy import deepcopy
    from skeleton.accounts.providers import ProviderConfiguration
    configuration, transport = govbr
    other = replace(configuration.bindings[0], provider_id='govbr-other',
                    app=deepcopy(configuration.bindings[0].app))
    other.app['provider_id'] = other.provider_id
    provider_settings = deepcopy(configuration.settings)
    provider_settings['openid_connect']['APPS'].append(other.app)
    both = ProviderConfiguration(provider_settings, (configuration.bindings[0], other))
    client = Client()
    authorization = start(client)
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=both, SOCIALACCOUNT_PROVIDERS=provider_settings):
        client.get('/accounts/oidc/govbr-other/login/callback/',
                   {'code': 'fixture-code', 'state': authorization['state'][0]})
    assert not transport.token_requests


@pytest.mark.parametrize('deleted', [False, True])
def test_global_offboarding_cannot_be_undone_by_government_identity(govbr, deleted):
    from allauth.account.models import EmailAddress
    from skeleton.accounts.services import bootstrap_identity, create_identity, deactivate_user
    configuration, transport = govbr
    with system_context(reason='test:govbr-offboarding'):
        user = create_identity(email='citizen@example.test', password=None)
        EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
        SocialAccount.objects.create(user=user, provider='govbr-staging', uid='00000000000',
                                     extra_data={'skeleton_binding': configuration.bindings[0].fingerprint})
    admin, _ = bootstrap_identity(email='operator@example.test', organization='govbr-offboarding',
                                  name='Offboarding', password=PASSWORD)
    with request_context(actor=admin, request_id=None, source='test:govbr-offboarding'):
        deactivate_user(user, delete=deleted)
    client = Client()
    authorization = start(client)
    transport.prepare(authorization)
    assert finish(client, authorization)['Location'] == '/accounts/3rdparty/login/error/'
    assert client.get('/api/v1/me/').status_code == 403
    user.refresh_from_db()
    assert not user.is_active and bool(user.deleted_at) is deleted


def test_platform_mfa_remains_required_after_government_login(govbr):
    from skeleton.accounts.services import bootstrap_identity
    configuration, transport = govbr
    user, _ = bootstrap_identity(email='citizen@example.test', organization='govbr-platform',
                                 name='Platform', password=PASSWORD)
    with system_context(reason='test:govbr-platform-connect'):
        SocialAccount.objects.create(user=user, provider='govbr-staging', uid='00000000000',
                                     extra_data={'skeleton_binding': configuration.bindings[0].fingerprint})
    client = Client()
    authorization = start(client)
    transport.prepare(authorization)
    assert finish(client, authorization).status_code == 302
    with override_settings(SKELETON_REQUIRE_ADMIN_MFA=True):
        response = client.get('/admin/')
        assert response.status_code == 302 and '/accounts/2fa/' in response['Location']
    user.refresh_from_db()
    assert user.is_staff and user.is_superuser  # claims cannot demote an existing identity either
