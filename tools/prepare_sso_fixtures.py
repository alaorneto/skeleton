"""Fetch live Keycloak public metadata and generate private loopback test configuration.

Import tests/sso/realm-skeleton-{a,b}.json into Keycloak 26.8.0 first.
This does not provision users in Django, modify a trust store or copy IdP keys.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
from urllib.request import urlopen
import xml.etree.ElementTree as ET

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def fetch(url):
    with urlopen(url, timeout=15) as response:
        return response.read()


def prepare(output):
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    providers, environment = [], {}
    for suffix in ('a', 'b'):
        issuer = f'http://127.0.0.1:8080/realms/skeleton-{suffix}'
        discovery = json.loads(fetch(issuer + '/.well-known/openid-configuration'))
        if discovery['issuer'] != issuer or 'S256' not in discovery.get('code_challenge_methods_supported', []):
            raise RuntimeError('Keycloak fixture issuer or PKCE S256 configuration is incorrect.')
        oidc = f'oidc-{suffix}'
        secret_name = f'SKELETON_FIXTURE_OIDC_{suffix.upper()}_SECRET'
        environment[secret_name] = f'fixture-only-oidc-secret-{suffix}'
        providers.append({'protocol':'oidc','provider_id':oidc,'organization_slug':f'sso-{suffix}',
            'jit_provisioning':True,'default_role_code':'member','trust_verified_email':True,
            'app':{'name':oidc,'client_id':oidc,'secret_env':secret_name,
                'settings':{'server_url':issuer,'uid_field':'sub','oauth_pkce_enabled':True,
                    'token_auth_method':'client_secret_basic'}}})
        metadata_url = issuer + '/protocol/saml/descriptor'
        root = ET.fromstring(fetch(metadata_url))
        ns = {'md':'urn:oasis:names:tc:SAML:2.0:metadata','ds':'http://www.w3.org/2000/09/xmldsig#'}
        signing = root.find('.//md:KeyDescriptor[@use="signing"]//ds:X509Certificate', ns)
        if signing is None or not signing.text:
            raise RuntimeError('Live Keycloak metadata has no signing certificate.')
        certificate = ''.join(signing.text.split())
        # Validate public certificate encoding, not the protocol assertion ourselves.
        x509.load_pem_x509_certificate(('-----BEGIN CERTIFICATE-----\n'+certificate+'\n-----END CERTIFICATE-----\n').encode())
        sso = root.find('.//md:SingleSignOnService[@Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"]', ns)
        if sso is None:
            raise RuntimeError('Keycloak metadata lacks Redirect SSO binding.')
        saml = f'saml-{suffix}'
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f'Skeleton fixture {saml}')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
            .not_valid_after(now+timedelta(days=30)).sign(key, hashes.SHA256()))
        private_name = f'SKELETON_FIXTURE_SAML_{suffix.upper()}_PRIVATE_KEY'
        environment[private_name] = key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        providers.append({'protocol':'saml','provider_id':saml,'organization_slug':f'sso-{suffix}',
            'jit_provisioning':True,'default_role_code':'member','trust_verified_email':False,
            'app':{'name':saml,'client_id':saml,'settings':{'idp':{'entity_id':root.attrib['entityID'],
                'sso_url':sso.attrib['Location'],'x509cert':certificate},
                'sp':{'entity_id':f'http://127.0.0.1:8000/accounts/saml/{saml}/metadata/'},
                'attribute_mapping':{'email':'email','first_name':'first_name','last_name':'last_name'},
                'advanced':{'strict':True,'want_assertion_signed':True,'want_message_signed':True,
                    'x509cert':cert.public_bytes(serialization.Encoding.PEM).decode(), 'private_key_env':private_name,
                    'reject_deprecated_algorithm':True,'reject_idp_initiated_sso':True}}}})
    # JSON can represent multiline PEM safely. Tests load it before loading Django settings.
    for filename, data in (('providers.json',{'providers':providers}),('environment.json',environment)):
        path = output / filename
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
        os.chmod(path, 0o600)
    exports = dict(environment, SSO_PROVIDER_CONFIG_PATH=str(output / 'providers.json'),
        SSO_FIXTURE_ENV_PATH=str(output / 'environment.json'))
    shell_path = output / 'environment.sh'
    descriptor = os.open(shell_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        for name, value in exports.items():
            stream.write(f'export {name}={shlex.quote(value)}\n')
    os.chmod(shell_path, 0o600)
    return output / 'providers.json', output / 'environment.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.sso-fixtures'))
    args = parser.parse_args()
    providers, environment = prepare(args.output.resolve())
    print(f'SSO_PROVIDER_CONFIG_PATH={providers}')
    print(f'SSO_FIXTURE_ENV_PATH={environment}')
    print(f'Source private exports before loading Django: source {shlex.quote(str(providers.parent / "environment.sh"))}')
    print('No protocol verification has run.')


if __name__ == '__main__':
    main()
