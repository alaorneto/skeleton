"""Identity providers stored in the database, resolved per request by allauth (#29)."""

import json
from io import StringIO
from uuid import uuid4

import pytest
from allauth.core import context
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialApp
from cryptography.fernet import Fernet
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory, override_settings

from skeleton.accounts import identity_providers
from skeleton.accounts.adapters import SocialAccountAdapter
from skeleton.accounts.providers import admission_for, configured_providers, load_provider_config
from skeleton.accounts.services import bootstrap_identity
from skeleton.accounts.social_providers import MinimalOpenIDConnectProvider
from skeleton.common.exceptions import ImmutableRecord
from skeleton.tenancy.context import request_context, system_context

KEY = Fernet.generate_key().decode()
PASSWORD = "Identity-provider-db-Password!29"


def file_entries():
    return [
        {
            "protocol": "oidc",
            "provider_id": "acme-oidc",
            "organization_slug": "idp-acme",
            "jit_provisioning": True,
            "trust_verified_email": True,
            "app": {
                "name": "Acme OIDC",
                "client_id": "acme-client",
                "secret_env": "IDP_DB_SECRET",
                "settings": {"server_url": "https://idp.example.test/realms/acme/"},
            },
        },
        {
            "protocol": "saml",
            "provider_id": "acme-saml",
            "organization_slug": "idp-acme",
            "app": {
                "name": "Acme SAML",
                "client_id": "acme-saml",
                "settings": {
                    "idp": {
                        "entity_id": "https://saml.example.test/idp",
                        "sso_url": "https://saml.example.test/sso",
                        "x509cert": "MIIC-old",
                    },
                    "sp": {"entity_id": "https://app.example.test/saml"},
                },
            },
        },
    ]


def load_file(tmp_path, monkeypatch, entries=None):
    monkeypatch.setenv("IDP_DB_SECRET", "file-client-secret")
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"providers": file_entries() if entries is None else entries}))
    return path, load_provider_config(path)


def empty():
    return load_provider_config(None)


def http_request(path="/accounts/login/"):
    req = RequestFactory().get(path)
    SessionMiddleware(lambda req: None).process_request(req)
    req.user = AnonymousUser()
    from django.contrib.messages.storage.fallback import FallbackStorage

    req._messages = FallbackStorage(req)
    return req


@pytest.fixture
def org(skeleton_db):
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[KEY]):
        admin, organization = bootstrap_identity(
            email="idp-owner@example.test", organization="idp-acme", name="IdP Acme", password=PASSWORD
        )
        yield admin, organization


def create(organization, **extra):
    data = {
        "provider_id": "db-oidc",
        "protocol": "oidc",
        "bound_organization": organization,
        "issuer": "https://login.example.test/realms/acme",
        "client_id": "db-client",
        "secret": "db-client-secret",
        "name": "Acme DB",
        **extra,
    }
    with system_context(reason="test:idp-create"):
        return identity_providers.create_identity_provider(actor=None, data=data)


def test_db_fingerprint_matches_the_file_loader(tmp_path, monkeypatch):
    _, configuration = load_file(tmp_path, monkeypatch)
    oidc, saml = configuration.bindings
    assert oidc.fingerprint == identity_providers.binding_fingerprint(
        "oidc", "https://idp.example.test/realms/acme", "acme-client", "idp-acme"
    )
    assert saml.fingerprint == identity_providers.binding_fingerprint(
        "saml", "https://saml.example.test/idp", "acme-saml", "idp-acme", "https://app.example.test/saml"
    )


def test_settings_allowlist_per_protocol():
    assert identity_providers.normalize_settings("oidc", {}) == {}
    for protocol, options in (
        ("oidc", {"scope": ["openid", "admin"]}),
        ("oidc", {"verified_email": True}),
        ("saml", {"idp": {"x509cert": "c", "private_key": "secret"}}),
        ("saml", {"advanced": {"strict": False}}),
        ("saml", {"idp": {"entity_id": "x"}}),
        ("saml", {"idp": {"sso_url": "http://insecure.example.test/sso", "x509cert": "c"}}),
        ("saml", {}),
    ):
        with pytest.raises(ValidationError):
            identity_providers.normalize_settings(protocol, options)


def test_db_provider_logs_in_on_the_next_request_and_disabling_blocks_it(org):
    _, organization = org
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=empty(), SOCIALACCOUNT_PROVIDERS=empty().settings):
        req = http_request()
        assert all(app.provider_id != "db-oidc" for app in SocialAccountAdapter().list_apps(req))
        row = create(organization)
        req = http_request()
        with (
            request_context(actor=None, request_id=uuid4(), source="test:idp-next-request"),
            context.request_context(req),
        ):
            provider = SocialAccountAdapter().get_provider(req, "db-oidc")
            assert isinstance(provider, MinimalOpenIDConnectProvider)
            assert (provider.app.client_id, provider.app.secret) == ("db-client", "db-client-secret")
            assert provider.app.settings["server_url"] == "https://login.example.test/realms/acme"
            assert configured_providers().get("db-oidc").fingerprint == row.fingerprint
        with system_context(reason="test:idp-disable"):
            identity_providers.update_identity_provider(
                actor=None, provider_id=row.pk, data={"enabled": False}, expected_revision=row.revision
            )
        req = http_request()
        with request_context(actor=None, request_id=uuid4(), source="test:idp-disabled"), context.request_context(req):
            with pytest.raises(SocialApp.DoesNotExist):
                SocialAccountAdapter().get_app(req, "db-oidc")
            with pytest.raises(PermissionDenied):
                MinimalOpenIDConnectProvider(
                    req, app=SocialApp(provider="openid_connect", provider_id="db-oidc")
                )._binding()


def test_identity_is_immutable_in_the_guard_and_the_service(org):
    from skeleton.accounts.models import IdentityProvider

    admin, organization = org
    row = create(organization)
    other, _ = bootstrap_identity(email="idp-other@example.test", organization="idp-other", name="O", password=PASSWORD)
    from skeleton.tenancy.models import Organization

    second = Organization.objects.get(slug="idp-other")
    for field, value in (
        ("provider_id", "renamed"),
        ("protocol", "saml"),
        ("bound_organization", second),
        ("issuer", "https://evil.example.test"),
        ("client_id", "other-client"),
    ):
        fresh = IdentityProvider.objects.get(pk=row.pk)
        setattr(fresh, field, value)
        with system_context(reason="test:idp-immutable"), pytest.raises(ImmutableRecord):
            fresh.save()
        with system_context(reason="test:idp-immutable-service"), pytest.raises(ValidationError):
            identity_providers.update_identity_provider(
                actor=None, provider_id=row.pk, data={field: value}, expected_revision=row.revision
            )
    with system_context(reason="test:idp-guard"), connection.cursor() as cursor, pytest.raises(Exception):  # noqa: B017
        cursor.execute(
            "UPDATE skeleton.sk_accounts_identityprovider SET client_id='hijack', revision=revision+1 WHERE sysid=%s",
            [row.pk],
        )
    with system_context(reason="test:idp-demote"):
        other.is_staff = other.is_superuser = False
        other.save(update_fields=["is_staff", "is_superuser"])
    with pytest.raises(PermissionDenied):
        identity_providers.update_identity_provider(
            actor=other, provider_id=row.pk, data={"name": "x"}, expected_revision=row.revision
        )


def test_secrets_are_encrypted_and_redacted(org):
    from skeleton.audit.models import AuditEvent

    admin, organization = org
    row = create(organization)
    with connection.cursor() as cursor:
        cursor.execute("SELECT secret FROM skeleton.sk_accounts_identityprovider WHERE sysid=%s", [row.pk])
        stored = cursor.fetchone()[0]
    assert stored and "db-client-secret" not in stored
    from skeleton.accounts.models import IdentityProvider

    loaded = IdentityProvider.objects.get(pk=row.pk).secret
    assert "db-client-secret" not in repr(loaded) and loaded.reveal() == "db-client-secret"
    with request_context(actor=admin, request_id=uuid4(), source="test:idp-audit"):
        changes = list(AuditEvent.objects.filter(record_sysid=row.pk).values_list("changes", flat=True))
    assert changes and "db-client-secret" not in json.dumps(changes)
    assert changes[0]["secret"]["redacted"] is True


def test_import_keeps_fingerprints_and_social_accounts(org, tmp_path, monkeypatch):
    from skeleton.accounts.models import IdentityProvider
    from skeleton.accounts.services import create_identity

    admin, organization = org
    path, configuration = load_file(tmp_path, monkeypatch)
    oidc = configuration.bindings[0]
    with system_context(reason="test:idp-subject"):
        user = create_identity(email="subject@example.test", password=None)
        SocialAccount.objects.create(
            user=user, provider="acme-oidc", uid="subject-1", extra_data={"skeleton_binding": oidc.fingerprint}
        )
    with override_settings(
        SKELETON_PROVIDER_CONFIGURATION=configuration, SOCIALACCOUNT_PROVIDERS=configuration.settings
    ):
        out = StringIO()
        call_command("import_identity_providers", "--from-config", str(path), "--dry-run", stdout=out)
        assert "would import 2" in out.getvalue() and not IdentityProvider.objects.exists()
        call_command("import_identity_providers", "--from-config", str(path), stdout=StringIO())
        rows = {row.provider_id: row for row in IdentityProvider.objects.all()}
        assert {pid: row.fingerprint for pid, row in rows.items()} == {
            binding.provider_id: binding.fingerprint for binding in configuration.bindings
        }
        assert rows["acme-oidc"].secret.reveal() == "file-client-secret"
        warnings = __import__("skeleton.accounts.providers", fromlist=["x"]).validate_provider_bindings()
        assert len(warnings) == 2  # Still in the file: transition warnings, not errors.
    # The entries leave the file: the database rows answer, with the same fingerprint.
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=empty(), SOCIALACCOUNT_PROVIDERS=empty().settings):
        req = http_request()
        with request_context(actor=None, request_id=uuid4(), source="test:idp-imported"):
            binding = configured_providers().get("acme-oidc")
            assert binding.source == "db" and binding.fingerprint == oidc.fingerprint
            assert admission_for(binding).jit_provisioning is True  # Imported from the file's rule.
            with context.request_context(req):
                provider = SocialAccountAdapter().get_provider(req, "acme-oidc")
                sociallogin = provider.sociallogin_from_response(
                    req, {"sub": "subject-1", "email": user.email, "email_verified": True}
                )
                sociallogin.lookup()
                assert sociallogin.is_existing and sociallogin.user.pk == user.pk
        from skeleton.accounts.providers import validate_provider_bindings

        assert validate_provider_bindings() == []


def test_saml_certificate_rotation_keeps_subjects(org):
    admin, organization = org
    row = create(
        organization,
        provider_id="db-saml",
        protocol="saml",
        issuer=None,
        entity_id="https://saml.example.test/idp",
        client_id="db-saml",
        sp_entity_id="https://app.example.test/saml",
        secret=None,
        settings={"idp": {"sso_url": "https://saml.example.test/sso", "x509cert": "MIIC-old"}},
    )
    with system_context(reason="test:idp-saml-subject"):
        from skeleton.accounts.services import create_identity

        user = create_identity(email="saml-subject@example.test", password=None)
        SocialAccount.objects.create(
            user=user, provider="db-saml", uid="saml-1", extra_data={"skeleton_binding": row.fingerprint}
        )
        rotated = identity_providers.update_identity_provider(
            actor=None,
            provider_id=row.pk,
            data={"settings": {"idp": {"sso_url": "https://saml.example.test/sso", "x509cert": "MIIC-new"}}},
            expected_revision=row.revision,
        )
    assert rotated.fingerprint == row.fingerprint and rotated.settings["idp"]["x509cert"] == "MIIC-new"
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=empty(), SOCIALACCOUNT_PROVIDERS=empty().settings):
        from skeleton.accounts.providers import validate_provider_bindings

        assert validate_provider_bindings() == []
        with request_context(actor=None, request_id=uuid4(), source="test:idp-saml"):
            app = configured_providers().get("db-saml").app
        assert app["settings"]["idp"]["x509cert"] == "MIIC-new"
        assert app["settings"]["advanced"]["want_assertion_signed"] is True


def test_jit_is_off_by_default_and_the_role_comes_from_the_organization(org):
    from skeleton.access.models import Role
    from skeleton.tenancy.models import IdentityProviderAdmission

    admin, organization = org
    row = create(organization)
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=empty(), SOCIALACCOUNT_PROVIDERS=empty().settings):
        with request_context(actor=None, request_id=uuid4(), source="test:idp-jit-off"):
            binding = configured_providers().get("db-oidc")
            assert admission_for(binding).jit_provisioning is False
        with system_context(reason="test:idp-admission", organization_id=organization.pk):
            role = Role.objects.get(code="app_manager", is_builtin=True)
            IdentityProviderAdmission(
                organization=organization, provider=row, jit_provisioning=True, default_role=role
            ).save()
        with request_context(actor=None, request_id=uuid4(), source="test:idp-jit-on"):
            binding = configured_providers().get("db-oidc")
            rules = admission_for(binding)
            assert rules.jit_provisioning is True and rules.default_role_id == role.pk
            resolved_org, resolved_role = SocialAccountAdapter()._resolve_binding(binding)
        assert resolved_org.pk == organization.pk and resolved_role.pk == role.pk


def test_existing_email_never_auto_connects_and_claims_never_promote(org):
    from skeleton.tenancy.models import IdentityProviderAdmission

    admin, organization = org
    row = create(organization)
    with system_context(reason="test:idp-admission-jit", organization_id=organization.pk):
        IdentityProviderAdmission(organization=organization, provider=row, jit_provisioning=True).save()
    with override_settings(SKELETON_PROVIDER_CONFIGURATION=empty(), SOCIALACCOUNT_PROVIDERS=empty().settings):
        req = http_request()
        with request_context(actor=None, request_id=uuid4(), source="test:idp-email"), context.request_context(req):
            provider = SocialAccountAdapter().get_provider(req, "db-oidc")
            existing = provider.sociallogin_from_response(
                req, {"sub": "new-subject", "email": admin.email, "email_verified": True}
            )
            existing.lookup()
            with pytest.raises(ImmediateHttpResponse):
                SocialAccountAdapter().pre_social_login(req, existing)
            fresh = provider.sociallogin_from_response(
                req,
                {
                    "sub": "fresh-subject",
                    "email": "fresh@example.test",
                    "email_verified": True,
                    "groups": ["platform-admins"],
                    "is_staff": True,
                    "is_superuser": True,
                },
            )
            fresh.lookup()
            fresh.user.is_staff = fresh.user.is_superuser = True  # Even if something mapped claims.
            SocialAccountAdapter().pre_social_login(req, fresh)
            assert fresh.user.is_staff is False and fresh.user.is_superuser is False
            assert fresh.account.extra_data == {"skeleton_binding": row.fingerprint}


def test_validation_reports_conflicts_and_undecryptable_secrets(org, tmp_path, monkeypatch):
    from skeleton.accounts.providers import validate_provider_bindings

    admin, organization = org
    path, configuration = load_file(tmp_path, monkeypatch)
    with override_settings(
        SKELETON_PROVIDER_CONFIGURATION=configuration, SOCIALACCOUNT_PROVIDERS=configuration.settings
    ):
        with pytest.raises(ValidationError):
            create(organization, provider_id="acme-oidc")  # The file has it with another identity.
    create(organization)
    with override_settings(
        SKELETON_PROVIDER_CONFIGURATION=empty(),
        SOCIALACCOUNT_PROVIDERS=empty().settings,
        IDENTITY_PROVIDER_ENCRYPTION_KEYS=[Fernet.generate_key().decode()],
    ):
        with pytest.raises(ImproperlyConfigured):
            validate_provider_bindings()
