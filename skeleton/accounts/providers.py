"""Administratively configured allauth providers; no database access at import time."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


# Official Login Único endpoints are a reviewed profile, not caller-controlled
# discovery metadata. Keep staging and production identity domains separate.
GOVBR_ISSUERS = {
    'staging': 'https://sso.staging.acesso.gov.br/',
    'production': 'https://sso.acesso.gov.br/',
}
GOVBR_SCOPES = ('openid', 'email', 'profile')


def govbr_openid_configuration(environment):
    issuer = GOVBR_ISSUERS[environment]
    return {
        'issuer': issuer,
        'authorization_endpoint': issuer + 'authorize',
        'token_endpoint': issuer + 'token',
        'userinfo_endpoint': issuer + 'userinfo',
        'jwks_uri': issuer + 'jwk',
        'end_session_endpoint': issuer + 'logout',
        'token_endpoint_auth_methods_supported': ['client_secret_basic'],
    }

@dataclass(frozen=True)
class ProviderBinding:
    protocol: str
    provider_id: str
    organization_slug: str | None
    jit_provisioning: bool
    default_role_code: str
    trust_verified_email: bool
    fingerprint: str
    app: dict


@dataclass(frozen=True)
class ProviderConfiguration:
    settings: dict
    bindings: tuple

    def get(self, provider_id):
        return next((item for item in self.bindings if item.provider_id == provider_id), None)


def _url(value, *, debug):
    if not isinstance(value, str):
        raise ImproperlyConfigured('Provider URL must be a string.')
    parsed = urlparse(value)
    if parsed.username or parsed.password or not parsed.hostname:
        raise ImproperlyConfigured('Provider URL must not contain credentials.')
    if parsed.scheme != 'https' and not (debug and parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}):
        raise ImproperlyConfigured('Provider endpoints require HTTPS (except loopback development).')


def _resolve_secrets(value):
    if isinstance(value, list):
        return [_resolve_secrets(item) for item in value]
    if not isinstance(value, dict):
        return value
    if any(key in value for key in ('secret', 'private_key')):
        raise ImproperlyConfigured('Provider secrets must use environment references.')
    result = {}
    for key, item in value.items():
        if key in {'secret_env', 'private_key_env'}:
            if not isinstance(item, str) or not os.environ.get(item):
                raise ImproperlyConfigured('Missing provider secret environment reference.')
            result['secret' if key == 'secret_env' else 'private_key'] = os.environ[item]
        else:
            result[key] = _resolve_secrets(item)
    return result


def load_provider_config(path=None, *, debug=False):
    # allauth caches provider classes when its registry first loads, even if
    # there are no configured apps yet. Security extensions must be installed
    # independently of credentials and the set of configured identities.
    providers = {
        identifier: {
            'APPS': [],
            'provider_class': 'skeleton.accounts.social_providers.' + name,
        }
        for identifier, name in (
            ('openid_connect', 'MinimalOpenIDConnectProvider'),
            ('saml', 'MinimalSAMLProvider'),
            ('github', 'MinimalGitHubProvider'),
        )
    }
    path = path or os.environ.get('SSO_PROVIDER_CONFIG_PATH')
    if not path:
        return ProviderConfiguration(providers, ())
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ImproperlyConfigured('Unable to read private SSO provider configuration.') from exc
    if not isinstance(document, dict) or set(document) != {'providers'} or not isinstance(document['providers'], list):
        raise ImproperlyConfigured('SSO configuration requires a providers list.')
    bindings, ids = [], set()
    allowed = {'protocol', 'provider_id', 'organization_slug', 'jit_provisioning', 'default_role_code', 'trust_verified_email', 'app', 'environment', 'post_logout_redirect_uri'}
    for entry in document['providers']:
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise ImproperlyConfigured('Unknown provider configuration fields.')
        protocol, provider_id = entry.get('protocol'), entry.get('provider_id')
        if protocol not in {'oidc', 'govbr', 'saml', 'github'} or not isinstance(provider_id, str) or not provider_id or provider_id in ids:
            raise ImproperlyConfigured('Provider protocol and unique nonempty provider_id are required.')
        if not all(character.isalnum() or character in '-_' for character in provider_id):
            raise ImproperlyConfigured('Provider ID must be URL-safe.')
        ids.add(provider_id)
        org = entry.get('organization_slug')
        if protocol in {'oidc', 'saml'} and (not isinstance(org, str) or not org):
            raise ImproperlyConfigured('Corporate SSO requires organization_slug.')
        if org is not None and (not isinstance(org, str) or not org):
            raise ImproperlyConfigured('organization_slug must be a nonempty string.')
        if protocol != 'govbr' and {'environment', 'post_logout_redirect_uri'} & set(entry):
            raise ImproperlyConfigured('Government identity settings require the govbr profile.')
        if protocol == 'govbr':
            if any(key in entry for key in ('organization_slug', 'jit_provisioning', 'default_role_code')):
                raise ImproperlyConfigured('Gov.br is global identity and cannot configure organizational admission.')
            if not isinstance(entry.get('environment'), str) or entry['environment'] not in GOVBR_ISSUERS:
                raise ImproperlyConfigured('Gov.br requires staging or production environment.')
            callback = entry.get('post_logout_redirect_uri')
            _url(callback, debug=False)
            parsed_callback = urlparse(callback)
            if parsed_callback.query or parsed_callback.fragment or parsed_callback.params:
                raise ImproperlyConfigured('Gov.br logout callback must be a fixed HTTPS URL without parameters.')
        for flag in ('jit_provisioning', 'trust_verified_email'):
            if flag in entry and type(entry[flag]) is not bool:
                raise ImproperlyConfigured(f'{flag} must be boolean.')
        role = entry.get('default_role_code', 'member')
        if not isinstance(role, str) or not role:
            raise ImproperlyConfigured('default_role_code is required.')
        app = deepcopy(entry.get('app'))
        if not isinstance(app, dict) or set(app) - {'name', 'client_id', 'secret_env', 'settings'}:
            raise ImproperlyConfigured('Provider app requires declared allauth APPS fields.')
        if not isinstance(app.get('client_id'), str) or not app['client_id']:
            raise ImproperlyConfigured('Provider client_id is required.')
        app = _resolve_secrets(app)
        app['provider_id'] = provider_id
        options = app.setdefault('settings', {})
        if not isinstance(options, dict):
            raise ImproperlyConfigured('Provider app settings must be an object.')
        allowed_options = {
            'oidc': {'server_url', 'uid_field', 'oauth_pkce_enabled', 'token_auth_method'},
            'govbr': set(),
            'saml': {'idp', 'sp', 'advanced', 'attribute_mapping', 'uid_field'},
            'github': set(),
        }
        if set(options) - allowed_options[protocol]:
            raise ImproperlyConfigured('Provider settings may not override email proof, scopes or protocol parameters.')
        if protocol == 'oidc':
            _url(options.get('server_url'), debug=debug)
            options['uid_field'] = 'sub'
            options['oauth_pkce_enabled'] = True
            options['token_auth_method'] = 'client_secret_basic'
            identity = options['server_url'].rstrip('/')
        elif protocol == 'govbr':
            if not app.get('secret'):
                raise ImproperlyConfigured('Gov.br requires a client secret environment reference.')
            environment = entry['environment']
            identity = GOVBR_ISSUERS[environment]
            app['name'] = 'GOV.BR'
            options.update({
                'server_url': identity,
                'uid_field': 'sub',
                'oauth_pkce_enabled': True,
                'token_auth_method': 'client_secret_basic',
                'fetch_userinfo': False,
                'govbr_environment': environment,
                'govbr_logout_redirect_uri': callback,
            })
        elif protocol == 'saml':
            idp, sp = options.get('idp'), options.setdefault('sp', {})
            if not isinstance(idp, dict) or not idp.get('entity_id') or not app.get('client_id'):
                raise ImproperlyConfigured('SAML requires client_id and idp.entity_id.')
            for key in ('sso_url', 'slo_url', 'metadata_url'):
                if idp.get(key):
                    _url(idp[key], debug=debug)
            if not idp.get('x509cert') and not idp.get('metadata_url'):
                raise ImproperlyConfigured('SAML requires trusted IdP metadata/certificate.')
            if not isinstance(sp, dict):
                raise ImproperlyConfigured('SAML SP settings must be an object.')
            advanced = options.setdefault('advanced', {})
            if not isinstance(advanced, dict):
                raise ImproperlyConfigured('SAML advanced settings must be an object.')
            advanced.update({'strict': True, 'want_assertion_signed': True, 'want_message_signed': True, 'reject_deprecated_algorithm': True, 'reject_idp_initiated_sso': True})
            identity = idp['entity_id']
        else:
            if provider_id != 'github':
                raise ImproperlyConfigured('The GitHub provider ID must be github.')
            identity = 'https://github.com'
        identity_tuple = ['skeleton-binding-v2', protocol, identity, app['client_id'], org or '',
                          options.get('sp', {}).get('entity_id', '') if protocol == 'saml' else '']
        if protocol == 'govbr':
            identity_tuple.append(entry['environment'])
        fingerprint = hashlib.sha256(json.dumps(identity_tuple, separators=(',', ':')).encode()).hexdigest()
        binding = ProviderBinding(protocol, provider_id, org, entry.get('jit_provisioning', False), role, entry.get('trust_verified_email', False), fingerprint, app)
        bindings.append(binding)
        provider = providers[{'oidc': 'openid_connect', 'govbr': 'openid_connect', 'saml': 'saml', 'github': 'github'}[protocol]]
        provider['APPS'].append(binding.app)
        if protocol == 'github':
            provider['SCOPE'] = ['user:email']
        if protocol in {'oidc', 'govbr'}:
            provider['OAUTH_PKCE_ENABLED'] = True
            provider['SCOPE'] = ['openid', 'profile', 'email']
    return ProviderConfiguration(providers, tuple(bindings))


def configured_providers():
    configuration = getattr(settings, 'SKELETON_PROVIDER_CONFIGURATION', None)
    if configuration is None:
        raise ImproperlyConfigured('SSO provider configuration must be materialized at startup.')
    return configuration




def binding_for_sociallogin(sociallogin):
    provider = sociallogin.account.get_provider()
    identifier = getattr(provider, 'sub_id', None) or provider.id
    binding = configured_providers().get(identifier)
    if binding is None:
        raise ImproperlyConfigured('SSO provider is not administratively configured.')
    return binding


def validate_provider_bindings(*, using='default'):
    from allauth.socialaccount.models import SocialAccount
    from skeleton.access.models import Role
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Organization
    for binding in configured_providers().bindings:
        if binding.organization_slug:
            organization = Organization.objects.using(using).filter(slug=binding.organization_slug, is_active=True).first()
            if organization is None:
                raise ImproperlyConfigured(f'Provider {binding.provider_id}: active organization does not exist.')
            with system_context(reason='sso:validated', organization_id=organization.pk, using=using):
                if not Role.objects.using(using).filter(code=binding.default_role_code).exists():
                    raise ImproperlyConfigured(f'Provider {binding.provider_id}: active default role does not exist.')
        for data in SocialAccount.objects.using(using).filter(provider=binding.provider_id).values_list('extra_data', flat=True):
            if data.get('skeleton_binding') != binding.fingerprint:
                raise ImproperlyConfigured(f'Provider {binding.provider_id}: issuer/entity or organization binding changed.')
