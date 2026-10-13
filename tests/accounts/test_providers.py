import json

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
import pytest

from skeleton.accounts.adapters import EncryptedMFAAdapter
from skeleton.accounts.providers import load_provider_config


def _provider(**extra):
    entry = {'protocol': 'oidc', 'provider_id': 'acme', 'organization_slug': 'acme', 'app': {'name': 'Acme', 'client_id': 'skeleton', 'secret_env': 'FIXTURE_OIDC_SECRET', 'settings': {'server_url': 'https://idp.example.test/.well-known/openid-configuration'}}}
    entry.update(extra)
    return entry


def test_parser_resolves_secrets_without_exposing_claims(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_OIDC_SECRET', 'fixture-secret')
    path = tmp_path / 'providers.json'
    path.write_text(json.dumps({'providers': [_provider()]}))
    config = load_provider_config(path)
    app = config.settings['openid_connect']['APPS'][0]
    assert app['secret'] == 'fixture-secret'
    assert app['settings']['uid_field'] == 'sub'
    assert config.settings['openid_connect']['OAUTH_PKCE_ENABLED']
    assert config.settings['openid_connect']['provider_class'].endswith('MinimalOpenIDConnectProvider')
    assert not config.bindings[0].jit_provisioning


@pytest.mark.parametrize('entry', [_provider(protocol='unknown'), _provider(organization_slug=None), _provider(jit_provisioning='true'), _provider(provider_id='../arbitrary')])
def test_invalid_configuration_fails_closed(tmp_path, monkeypatch, entry):
    monkeypatch.setenv('FIXTURE_OIDC_SECRET', 'fixture-secret')
    path = tmp_path / 'providers.json'
    path.write_text(json.dumps({'providers': [entry]}))
    with pytest.raises(ImproperlyConfigured):
        load_provider_config(path)


def test_loopback_http_only_debug(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_OIDC_SECRET', 'fixture-secret')
    entry = _provider()
    entry['app']['settings']['server_url'] = 'http://127.0.0.1:8080/realms/acme/.well-known/openid-configuration'
    path = tmp_path / 'providers.json'
    path.write_text(json.dumps({'providers': [entry]}))
    with pytest.raises(ImproperlyConfigured):
        load_provider_config(path)
    assert load_provider_config(path, debug=True).bindings


def test_fernet_rotation_reads_old_but_writes_new():
    old, new = Fernet.generate_key(), Fernet.generate_key()
    with override_settings(MFA_ENCRYPTION_KEYS=[old]):
        value = EncryptedMFAAdapter().encrypt('fixture-mfa-secret')
    with override_settings(MFA_ENCRYPTION_KEYS=[new, old]):
        assert EncryptedMFAAdapter().decrypt(value) == 'fixture-mfa-secret'
        written = EncryptedMFAAdapter().encrypt('new-secret')
    assert Fernet(new).decrypt(written.encode()) == b'new-secret'


def test_missing_mfa_key_never_falls_back_to_secret_key():
    with override_settings(MFA_ENCRYPTION_KEYS=[], SECRET_KEY='not-an-encryption-key'):
        with pytest.raises(ImproperlyConfigured):
            EncryptedMFAAdapter().encrypt('secret')
