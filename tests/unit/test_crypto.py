"""Deployment keyrings and the write-only encrypted field (#19)."""

import pytest
from cryptography.fernet import Fernet, InvalidToken
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test import override_settings
from django.test.utils import isolate_apps

from skeleton.common.crypto import (
    IDENTITY_PROVIDER_KEYS,
    EncryptedTextField,
    SealedSecret,
    multifernet,
    validate_keyring,
)

OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()
FIELD = EncryptedTextField()


def test_empty_or_invalid_keyring_is_a_configuration_error():
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[]):
        with pytest.raises(ImproperlyConfigured, match="plaintext identity provider is forbidden"):
            multifernet(IDENTITY_PROVIDER_KEYS, purpose="identity provider")
        with pytest.raises(ImproperlyConfigured):
            FIELD.get_prep_value("client-secret")  # No plaintext fallback.
        validate_keyring(IDENTITY_PROVIDER_KEYS, purpose="identity provider")  # Empty is fine until used.
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=["not-a-fernet-key"]):
        with pytest.raises(ImproperlyConfigured, match="Invalid identity provider Fernet key"):
            validate_keyring(IDENTITY_PROVIDER_KEYS, purpose="identity provider")


def test_round_trip_never_exposes_the_plaintext():
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[OLD]):
        token = FIELD.get_prep_value("client-secret")
        assert "client-secret" not in token
        sealed = FIELD.from_db_value(token, None, None)
        assert sealed.reveal() == "client-secret"
        for shown in (repr(sealed), str(sealed), f"{sealed}", str([sealed])):
            assert "client-secret" not in shown and token not in shown
        assert FIELD.get_prep_value(sealed) == token  # Unchanged secrets are stored as they are.
        assert FIELD.get_prep_value(None) is None
        with pytest.raises(TypeError):
            FIELD.get_prep_value(123)


def test_rotation_keeps_reads_after_the_old_key_is_removed():
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[OLD]):
        sealed = FIELD.from_db_value(FIELD.get_prep_value("saml-private-key"), None, None)
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[NEW, OLD]):
        rotated = FIELD.rotate(sealed)
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[NEW]):
        assert rotated.reveal() == "saml-private-key"
        with pytest.raises(ImproperlyConfigured, match="retain old keys"):
            sealed.reveal()
        with pytest.raises(InvalidToken):
            FIELD.rotate(sealed)


def test_keyrings_are_separate_and_field_deconstructs():
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[OLD], MFA_ENCRYPTION_KEYS=[NEW]):
        mfa_field = EncryptedTextField(keyring="MFA_ENCRYPTION_KEYS")
        sealed = SealedSecret(FIELD.get_prep_value("x"), keyring=IDENTITY_PROVIDER_KEYS)
        with pytest.raises(ImproperlyConfigured):
            mfa_field.get_prep_value(sealed)  # Never reinterpreted under another keyring.
    assert "keyring" not in FIELD.deconstruct()[3]
    assert EncryptedTextField(keyring="MFA_ENCRYPTION_KEYS").deconstruct()[3]["keyring"] == "MFA_ENCRYPTION_KEYS"


def test_checks_require_sensitive_declaration_and_valid_keys(monkeypatch):
    from skeleton.system import registry
    from skeleton.system.checks import encrypted_secret_checks

    with isolate_apps("skeleton.system.apps.SystemConfig") as isolated:

        class ProviderFixture(models.Model):
            client_secret = EncryptedTextField(null=True)

            class Meta:
                app_label = "sk_system"

        monkeypatch.setitem(
            registry._registry,
            ProviderFixture._meta.label_lower,
            registry.TableSpec(model=ProviderFixture, description="x", scope="global"),
        )
        ids = [error.id for error in encrypted_secret_checks(apps_registry=isolated)]
        assert ids == ["skeleton.E026"]
        monkeypatch.setitem(
            registry._registry,
            ProviderFixture._meta.label_lower,
            registry.TableSpec(
                model=ProviderFixture, description="x", scope="global", sensitive_fields=("client_secret",)
            ),
        )
        assert encrypted_secret_checks(apps_registry=isolated) == []
        with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=["bad"]):
            assert [error.id for error in encrypted_secret_checks(apps_registry=isolated)] == ["skeleton.E026"]
