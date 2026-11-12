"""Characterization of load_provider_config before and after its decomposition (#304).

The literals below were recorded from the implementation before the refactor: the same documents
must give the same allauth settings, bindings and fingerprints, and a document with two problems
must keep reporting the first one checked.
"""

import hashlib
import json

import pytest
from django.core.exceptions import ImproperlyConfigured

from skeleton.accounts.providers import load_provider_config

SECRETS = {
    "OIDC_C_SECRET": "oidc-secret",
    "SAML_C_SECRET": "saml-secret",
    "GITHUB_C_SECRET": "gh",
    "GOVBR_C_SECRET": "gov",
}


def oidc():
    return {
        "protocol": "oidc",
        "provider_id": "acme",
        "organization_slug": "acme",
        "jit_provisioning": True,
        "trust_verified_email": True,
        "app": {
            "name": "Acme",
            "client_id": "client-a",
            "secret_env": "OIDC_C_SECRET",
            "settings": {"server_url": "https://idp.example.test/realms/acme"},
        },
    }


def saml():
    return {
        "protocol": "saml",
        "provider_id": "corp-saml",
        "organization_slug": "corp",
        "app": {
            "name": "Corp",
            "client_id": "sp-client",
            "secret_env": "SAML_C_SECRET",
            "settings": {
                "idp": {"entity_id": "https://idp.corp.test", "x509cert": "public-certificate"},
                "sp": {"entity_id": "https://app.example.test/saml"},
            },
        },
    }


def github():
    return {
        "protocol": "github",
        "provider_id": "github",
        "app": {"name": "GitHub", "client_id": "gh-client", "secret_env": "GITHUB_C_SECRET"},
    }


def govbr():
    return {
        "protocol": "govbr",
        "provider_id": "govbr-staging",
        "environment": "staging",
        "post_logout_redirect_uri": "https://service.example.test/logout/",
        "trust_verified_email": True,
        "app": {"client_id": "fixture.gov.br", "secret_env": "GOVBR_C_SECRET"},
    }


def load(tmp_path, monkeypatch, providers):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"providers": providers}))
    return load_provider_config(path)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


# Recorded from the implementation before #304.
EXPECTED_SETTINGS_DIGEST = "d094fac0dc56f567a3947bf2bdec92bd1fd389f4c10f64a9fc27bfa97dc2b43d"
EXPECTED_BINDINGS = [
    ("oidc", "acme", "acme", True, "member", True, "cf871607d2316afa04e8da85c93d71ea4946bcac548ceaecae108f8aea8ce911"),
    (
        "saml",
        "corp-saml",
        "corp",
        False,
        "member",
        False,
        "121ea6897393f455ce924b23a93417cc54e91b01edd95dc8820001aeb4a91e27",
    ),
    (
        "github",
        "github",
        None,
        False,
        "member",
        False,
        "5c95bea5d56885dbbc177f9137941f216f39dd3c93674c8a661c3ea946f4f7f7",
    ),
    (
        "govbr",
        "govbr-staging",
        None,
        False,
        "member",
        True,
        "746646320fc3148fb843998046e31a281bbaf59ebd731cc288041df77538f2c9",
    ),
]


def test_load_provider_config_output_is_stable(tmp_path, monkeypatch):
    configuration = load(tmp_path, monkeypatch, [oidc(), saml(), github(), govbr()])
    bindings = [
        (
            b.protocol,
            b.provider_id,
            b.organization_slug,
            b.jit_provisioning,
            b.default_role_code,
            b.trust_verified_email,
            b.fingerprint,
        )
        for b in configuration.bindings
    ]
    assert digest(configuration.settings) == EXPECTED_SETTINGS_DIGEST
    assert bindings == EXPECTED_BINDINGS


def _two_problems():
    unknown = {**oidc(), "surprise": 1, "protocol": "bogus"}
    repeated = [oidc(), {**oidc(), "organization_slug": ""}]
    flags = {**oidc(), "jit_provisioning": "yes"}
    flags["app"] = {**flags["app"]}
    del flags["app"]["client_id"]
    options = oidc()
    options["app"]["settings"] = {"server_url": "http://idp.example.test", "scope": "everything"}
    saml_bad = saml()
    saml_bad["app"]["settings"]["idp"] = {}
    gov = {**govbr(), "organization_slug": "acme", "environment": "elsewhere"}
    return [
        ("unknown-and-protocol", [unknown], "Unknown provider configuration fields."),
        ("repeated-and-empty-slug", repeated, "Provider protocol and unique nonempty provider_id are required."),
        ("flag-and-client", [flags], "jit_provisioning must be boolean."),
        ("option-and-http", [options], "Provider settings may not override"),
        ("saml-entity-and-cert", [saml_bad], "SAML requires client_id and idp.entity_id."),
        ("govbr-org-and-environment", [gov], "Gov.br is global identity"),
    ]


@pytest.mark.parametrize(("case", "providers", "message"), _two_problems(), ids=[case[0] for case in _two_problems()])
def test_first_error_wins_for_documents_with_two_problems(tmp_path, monkeypatch, case, providers, message):
    with pytest.raises(ImproperlyConfigured) as raised:
        load(tmp_path, monkeypatch, providers)
    assert str(raised.value).startswith(message)
