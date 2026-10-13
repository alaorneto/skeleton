"""Live Keycloak browser exchanges, with real wire mutations and no protocol mocks.

Claim/destination/expiry mutations deliberately preserve original signatures:
these test refusal of tampered exchanges, not isolated re-signed IdP claims.
"""
import base64
from contextlib import contextmanager
from copy import deepcopy
import json
import re
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest
from django.test import override_settings

from .protocol_proxy import ExchangeProxy
from .database import db_call, database_thread

PASSWORD = 'Fixture-only-password-2026!'


@database_thread
def own_memberships(user):
    from skeleton.tenancy.context import request_context
    from skeleton.tenancy.models import Membership
    with request_context(actor=user,request_id=uuid4(),source='test:sso-discovery'):
        return list(Membership.all_objects.filter(user=user,is_active=True,deleted_at__isnull=True))


def submit_first_form(page):
    page.locator('form').first.locator('button[type=submit],input[type=submit],button:not([type])').first.click()
    page.wait_for_load_state('networkidle')


def login_at_idp(page, fixture, protocol='oidc', suffix='a', username=None, query=''):
    provider = f'{protocol}-{suffix}'
    path = f'/accounts/oidc/{provider}/login/' if protocol == 'oidc' else f'/accounts/saml/{provider}/login/'
    page.goto(fixture.origin + path + query)
    # Social login initiation remains POST with real CSRF, never LOGIN_ON_GET.
    if urlsplit(page.url).port == 8000:
        submit_first_form(page)
    if urlsplit(page.url).port == 8080:
        page.locator('#username').fill(username or provider)
        page.locator('#password').fill(PASSWORD)
        page.locator('#kc-login').click()
        page.wait_for_load_state('networkidle')
    assert urlsplit(page.url).port == 8000, 'Browser must complete the real IdP callback.'


def me(page, fixture):
    return page.request.get(fixture.origin + '/api/v1/me/')


def confirm_local_email(page, fixture, email):
    from django.core import mail
    messages = [message for message in getattr(mail,'outbox',[]) if email in message.to]
    assert messages, 'Untrusted protocol email must produce a real local confirmation message.'
    urls = re.findall(r'https?://[^\s<>\"]+/accounts/confirm-email/[^\s<>\"]+', messages[-1].body)
    assert urls, 'Confirmation link missing from materialized email.'
    page.goto(urls[0].rstrip('.,)'))
    submit_first_form(page)
    page.wait_for_load_state('networkidle')


@contextmanager
def provider_configuration(fixture, tmp_path, modify):
    from skeleton.accounts.providers import load_provider_config
    document = deepcopy(fixture.providers)
    modify(document['providers'])
    path = tmp_path / 'modified-providers.json'
    path.write_text(json.dumps(document))
    configuration = load_provider_config(str(path),debug=True)
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=configuration,SOCIALACCOUNT_PROVIDERS=configuration.settings):
        yield


@pytest.mark.parametrize('protocol,suffix',[('oidc','a'),('oidc','b'),('saml','a'),('saml','b')])
def test_real_browser_login_binding_email_and_idempotent_subject(page, real_sso, protocol, suffix):
    from skeleton.accounts.models import User, UserProfile
    from allauth.socialaccount.models import SocialAccount, SocialToken
    login_at_idp(page,real_sso,protocol,suffix)
    email = f'{protocol}-{suffix}@example.test'
    if protocol == 'saml':
        assert me(page,real_sso).status == 403
        # An additional real callback before local proof must retain the initial admission.
        login_at_idp(page,real_sso,protocol,suffix)
        assert me(page,real_sso).status == 403
        confirm_local_email(page,real_sso,email)
        # Email proof does not authenticate an anonymous session by itself.
        login_at_idp(page,real_sso,protocol,suffix)
    response = me(page,real_sso)
    assert response.status == 200, response.text()
    identity = response.json()
    user = db_call(lambda: User.objects.get(email=email))
    assert identity['sysid'] == str(user.pk)
    assert not user.is_staff and not user.is_superuser
    assert not user.local_password_allowed and not user.has_usable_password()
    assert db_call(lambda: UserProfile.objects.filter(user=user).count()) == 1
    memberships = own_memberships(user)
    assert len(memberships) == 1
    assert memberships[0].organization_id == real_sso.organizations[suffix].pk
    assert db_call(lambda: SocialAccount.objects.filter(user=user).count()) == 1
    account = db_call(lambda: SocialAccount.objects.get(user=user))
    assert not any(key in account.extra_data for key in ('access_token','refresh_token','id_token'))
    assert not db_call(lambda: SocialToken.objects.filter(account=account).exists())
    original_ids = (user.pk,db_call(lambda: UserProfile.objects.get(user=user).pk),memberships[0].pk,account.pk)
    organizations = page.request.get(real_sso.origin+'/api/v1/organizations/')
    assert organizations.status == 200
    assert str(real_sso.organizations[suffix].pk) in organizations.text()
    assert str(real_sso.organizations['b' if suffix=='a' else 'a'].pk) not in organizations.text()
    page.context.clear_cookies()
    login_at_idp(page,real_sso,protocol,suffix)
    assert me(page,real_sso).status == 200
    assert (db_call(lambda: User.objects.get(email=email).pk),db_call(lambda: UserProfile.objects.get(user=user).pk),own_memberships(user)[0].pk,db_call(lambda: SocialAccount.objects.get(user=user).pk)) == original_ids


@pytest.mark.parametrize('mutation',['signature','issuer','audience','expiry','pkce'])
def test_oidc_tampered_real_token_exchange_is_rejected(page, real_sso, tmp_path, mutation):
    from skeleton.accounts.models import User
    with ExchangeProxy(mutation) as proxy:
        def modify(providers):
            next(p for p in providers if p['provider_id']=='oidc-a')['app']['settings']['server_url'] = proxy.origin+'/realms/skeleton-a'
        with provider_configuration(real_sso,tmp_path,modify):
            login_at_idp(page,real_sso)
            assert proxy.token_requests, 'Negative must reach an actual Keycloak token exchange.'
            assert proxy.token_responses == ([400] if mutation=='pkce' else [200])
            assert me(page,real_sso).status == 403
            assert not db_call(lambda: User.objects.filter(email='oidc-a@example.test').exists())



@pytest.mark.parametrize('mutation', ['mismatch', 'missing'])
def test_oidc_real_idp_signed_nonce_must_match_one_use_state(page, real_sso, tmp_path, mutation):
    from skeleton.accounts.models import User
    authorizations = []

    def change_authorization(route):
        # Intercept the initial Django request, not a later URL in its redirect chain.
        response = route.fetch(max_redirects=0)
        parts = urlsplit(response.headers.get('location', ''))
        if parts.path != '/realms/skeleton-a/protocol/openid-connect/auth':
            route.fulfill(response=response)
            return
        values = parse_qs(parts.query)
        assert values.get('nonce'), 'The actual allauth authorization must include a nonce.'
        authorizations.append(values['nonce'][0])
        if mutation == 'missing':
            values.pop('nonce')
        else:
            values['nonce'] = ['nonce-changed-before-real-idp-signature']
        changed = urlunsplit((parts.scheme, parts.netloc, parts.path,
                             urlencode(values, doseq=True), ''))
        route.fulfill(response=response, headers={**response.headers, 'location': changed})

    pattern = re.compile(r'/accounts/oidc/oidc-a/login/(?:\?.*)?$')
    page.route(pattern, change_authorization)
    try:
        with ExchangeProxy() as proxy:
            def modify(providers):
                next(p for p in providers if p['provider_id'] == 'oidc-a')['app']['settings']['server_url'] = proxy.origin + '/realms/skeleton-a'
            with provider_configuration(real_sso, tmp_path, modify):
                login_at_idp(page, real_sso)
                assert authorizations and proxy.token_responses == [200]
                assert me(page, real_sso).status == 403
                assert not db_call(lambda: User.objects.filter(email='oidc-a@example.test').exists())
    finally:
        page.unroute(pattern, change_authorization)

def test_oidc_state_and_wrong_provider_callback_are_rejected(page,real_sso):
    from skeleton.accounts.models import User
    captured = []
    def mutate(route):
        # Browser routing sees the first URL of a redirect chain, not its callback.
        # Fetch the real IdP authentication response without following its 302.
        response = route.fetch(max_redirects=0)
        destination = response.headers.get('location', '')
        assert response.status == 302 and '/accounts/oidc/oidc-a/login/callback/' in destination
        captured.append(destination)
        parts = urlsplit(destination)
        values = parse_qs(parts.query)
        assert values.get('code') and values.get('state')
        values['state'] = ['invalid-state-from-real-exchange']
        changed = urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(values,doseq=True),''))
        route.fulfill(response=response, headers={**response.headers, 'location': changed})
    authentication_pattern = re.compile(r"/realms/skeleton-a/login-actions/authenticate")
    page.route(authentication_pattern,mutate)
    login_at_idp(page,real_sso)
    assert captured
    assert me(page,real_sso).status == 403
    assert not db_call(lambda: User.objects.filter(email='oidc-a@example.test').exists())
    page.unroute(authentication_pattern,mutate)
    # Replay the actual code under the other client/provider. No fabricated callback.
    page.goto(captured[0].replace('/oidc-a/','/oidc-b/'))
    assert me(page,real_sso).status == 403
    assert not db_call(lambda: User.objects.filter(email='oidc-a@example.test').exists())


def test_oidc_actual_authorization_code_and_callback_replay(page,real_sso,tmp_path):
    with ExchangeProxy() as proxy:
        def modify(providers):
            next(p for p in providers if p['provider_id']=='oidc-a')['app']['settings']['server_url'] = proxy.origin+'/realms/skeleton-a'
        callbacks = []
        page.on('request',lambda request: callbacks.append(request.url) if '/accounts/oidc/oidc-a/login/callback/' in request.url else None)
        with provider_configuration(real_sso,tmp_path,modify):
            login_at_idp(page,real_sso)
            assert me(page,real_sso).status == 200
            assert proxy.token_responses == [200]
            assert len(proxy.token_requests) == 1
            fields = proxy.token_requests[0]
            assert fields.get('code_verifier'), 'Successful code flow must use actual PKCE.'
            credentials = base64.b64encode(b'oidc-a:fixture-only-oidc-secret-a').decode()
            request = Request('http://127.0.0.1:8080/realms/skeleton-a/protocol/openid-connect/token',
                data=urlencode(fields,doseq=True).encode(),headers={'Authorization':'Basic '+credentials,'Content-Type':'application/x-www-form-urlencoded'})
            with pytest.raises(HTTPError) as failure:
                urlopen(request,timeout=10)
            assert failure.value.code == 400
            assert json.loads(failure.value.read())['error'] == 'invalid_grant'
            page.context.clear_cookies()
            assert callbacks
            page.goto(callbacks[0])
            assert me(page,real_sso).status == 403


def mutate_saml(encoded, mutation):
    document = base64.b64decode(encoded).decode()
    if mutation == 'signature':
        document,count = re.subn(r'(<(?:\w+:)?SignatureValue[^>]*>)[^<]+',r'\1YWJjZA==',document,count=1)
    elif mutation == 'destination':
        document,count = re.subn(r'Destination="[^"]+"','Destination="http://127.0.0.1:8000/wrong-destination/"',document,count=1)
    elif mutation == 'audience':
        document,count = re.subn(r'(<(?:\w+:)?Audience[^>]*>)[^<]+',r'\1wrong-audience',document,count=1)
    elif mutation == 'expiry':
        document,count = re.subn(r'NotOnOrAfter="[^"]+"','NotOnOrAfter="2000-01-01T00:00:00Z"',document)
    elif mutation == 'in_response_to':
        document,count = re.subn(r'InResponseTo="[^"]+"','InResponseTo="_unrelated-authn-request"',document)
    elif mutation == 'unsolicited':
        document,count = re.subn(r'\s+InResponseTo="[^"]+"','',document)
    else:
        raise ValueError(mutation)
    assert count, f'Real SAML fixture must contain the {mutation} target.'
    return base64.b64encode(document.encode()).decode()


@pytest.mark.parametrize('mutation',['signature','destination','audience','expiry','in_response_to','unsolicited'])
def test_saml_tampered_real_signed_exchange_is_rejected(page,real_sso,mutation):
    from skeleton.accounts.models import User
    captures = []
    def mutate(route):
        fields = parse_qs(route.request.post_data)
        assert fields.get('SAMLResponse')
        captures.append(fields['SAMLResponse'][0])
        fields['SAMLResponse'] = [mutate_saml(fields['SAMLResponse'][0],mutation)]
        route.continue_(post_data=urlencode(fields,doseq=True))
    page.route('**/accounts/saml/saml-a/acs/',mutate)
    login_at_idp(page,real_sso,protocol='saml')
    assert captures, 'Negative must capture an actual signed Keycloak assertion.'
    assert me(page,real_sso).status == 403
    assert not db_call(lambda: User.objects.filter(email='saml-a@example.test').exists())


def test_saml_real_assertion_replay_and_unsolicited_original_rejected(page,browser,real_sso):
    captures = []
    def capture(route):
        captures.append(parse_qs(route.request.post_data))
        route.continue_()
    page.route('**/accounts/saml/saml-a/acs/',capture)
    login_at_idp(page,real_sso,protocol='saml')
    confirm_local_email(page,real_sso,'saml-a@example.test')
    login_at_idp(page,real_sso,protocol='saml')
    assert me(page,real_sso).status == 200
    assert captures
    # Original, correctly signed response has no matching pending AuthnRequest in
    # this fresh session: rejection must not rely on broken signature mutations.
    fresh = browser.new_context()
    try:
        response = fresh.request.post(real_sso.origin+'/accounts/saml/saml-a/acs/',form={key:value[0] for key,value in captures[0].items()})
        assert response.status < 500
        assert fresh.request.get(real_sso.origin+'/api/v1/me/').status == 403
    finally:
        fresh.close()


@pytest.mark.parametrize('case',['no_jit','untrusted_email','unverified_email'])
def test_provider_admission_and_email_proof_are_not_claim_fallbacks(page,real_sso,tmp_path,case):
    from skeleton.accounts.models import User
    def modify(providers):
        entry = next(p for p in providers if p['provider_id']=='oidc-a')
        if case=='no_jit':
            entry['jit_provisioning'] = False
        elif case=='untrusted_email':
            entry['trust_verified_email'] = False
    username = 'unverified-a' if case=='unverified_email' else 'oidc-a'
    with provider_configuration(real_sso,tmp_path,modify):
        login_at_idp(page,real_sso,username=username)
        assert me(page,real_sso).status == 403
        user = db_call(lambda: User.objects.filter(email=f'{username}@example.test').first())
        if case=='no_jit':
            assert user is None
        elif user is not None:
            assert not own_memberships(user)


def test_email_collision_never_auto_connects_existing_account(page,real_sso):
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount
    from skeleton.accounts.services import create_identity
    from skeleton.accounts.models import User
    from skeleton.tenancy.context import system_context
    @database_thread
    def create_existing():
        with system_context(reason='sso-fixture:collision'):
            existing = create_identity(email='collision-a@example.test',password='Existing-fixture-password-2026!')
            EmailAddress.objects.create(user=existing,email=existing.email,verified=True,primary=True)
            return existing
    existing = create_existing()
    login_at_idp(page,real_sso,username='collision-a')
    assert me(page,real_sso).status == 403
    assert db_call(lambda: User.objects.filter(email=existing.email).count()) == 1
    assert not db_call(lambda: SocialAccount.objects.filter(user=existing).exists())


def test_client_tenant_and_promotion_claims_cannot_override_binding(page,real_sso):
    from skeleton.accounts.models import User
    from skeleton.access.models import MemberRole
    login_at_idp(page,real_sso,query='?organization_slug=sso-b&role=organization_admin&is_staff=true&is_superuser=true')
    assert me(page,real_sso).status == 200
    user = db_call(lambda: User.objects.get(email='oidc-a@example.test'))
    assert not user.is_staff and not user.is_superuser
    from tools.security_fixtures import actor_tenant
    @database_thread
    def effective_codes():
        with actor_tenant(user,real_sso.organizations['a']):
            return set(MemberRole.objects.filter(membership__user=user).values_list('role__code',flat=True))
    roles = effective_codes()
    assert roles == {'member'}
    orgs = page.request.get(real_sso.origin+'/api/v1/organizations/').text()
    assert str(real_sso.organizations['a'].pk) in orgs
    assert str(real_sso.organizations['b'].pk) not in orgs
