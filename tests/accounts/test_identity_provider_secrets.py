"""Identity provider secrets at rest: ciphertext in the database, offline rotation (#19)."""

import io

import pytest
from cryptography.fernet import Fernet
from django.core.management import CommandError, call_command
from django.db import connection, models
from django.test import override_settings
from django.test.utils import isolate_apps

from skeleton.common.crypto import EncryptedTextField, SealedSecret

pytestmark = pytest.mark.usefixtures("skeleton_db")
OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()


@pytest.fixture
def provider_table(monkeypatch):
    # Provider models arrive with #29: a TEMP table of the runtime session (TEMP is granted to
    # PUBLIC) stands in for them, so no owner credential and no DDL in the skeleton schema.
    with isolate_apps("skeleton.accounts.apps.AccountsConfig"):

        class ProviderSecret(models.Model):
            name = models.CharField(max_length=50)
            client_secret = EncryptedTextField(null=True)

            class Meta:
                app_label = "sk_accounts"
                db_table = "sk_test_providersecret"

        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TEMPORARY TABLE sk_test_providersecret "
                "(id serial PRIMARY KEY, name varchar(50) NOT NULL, client_secret text NULL)"
            )
        from skeleton.accounts.management.commands import rotate_identity_provider_keys
        from skeleton.common.crypto import IDENTITY_PROVIDER_KEYS

        found = [(ProviderSecret, ProviderSecret._meta.get_field("client_secret"))]

        def fields(keyring, registry=None):
            return found if keyring == IDENTITY_PROVIDER_KEYS else []

        monkeypatch.setattr(rotate_identity_provider_keys, "encrypted_fields", fields)
        monkeypatch.setattr("skeleton.common.crypto.encrypted_fields", fields)
        try:
            yield ProviderSecret
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP TABLE IF EXISTS pg_temp.sk_test_providersecret")


def raw_values(model):
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT client_secret FROM "{model._meta.db_table}" ORDER BY id')
        return [row[0] for row in cursor.fetchall()]


def test_database_holds_only_ciphertext_and_reads_are_sealed(provider_table):
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[OLD]):
        provider_table.objects.create(name="oidc", client_secret="very-secret-value")
        [stored] = raw_values(provider_table)
        assert "very-secret-value" not in stored
        loaded = provider_table.objects.get(name="oidc")
        assert isinstance(loaded.client_secret, SealedSecret)
        assert loaded.client_secret.reveal() == "very-secret-value"
        assert "very-secret-value" not in repr(loaded.__dict__)
        loaded.name = "oidc-renamed"
        loaded.save()
        assert raw_values(provider_table) == [stored]  # An untouched secret is not re-encrypted.


def test_rotation_command_rewrites_with_the_new_key_and_prints_only_counts_and_ids(provider_table):
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[OLD]):
        first = provider_table.objects.create(name="a", client_secret="secret-a")
        provider_table.objects.create(name="b", client_secret=None)
    output = io.StringIO()
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[NEW, OLD]):
        call_command("rotate_identity_provider_keys", stdout=output)
    text = output.getvalue()
    assert "rotated 1" in text and str(first.pk) in text and "secret-a" not in text
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[NEW]):
        assert provider_table.objects.get(pk=first.pk).client_secret.reveal() == "secret-a"


def test_rotation_without_the_old_key_changes_nothing(provider_table):
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[OLD]):
        provider_table.objects.create(name="a", client_secret="secret-a")
    before = raw_values(provider_table)
    with (
        override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[NEW]),
        pytest.raises(CommandError, match="retain all old"),
    ):
        call_command("rotate_identity_provider_keys", stdout=io.StringIO())
    assert raw_values(provider_table) == before
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[]), pytest.raises(Exception, match="IDENTITY_PROVIDER"):
        call_command("rotate_identity_provider_keys", stdout=io.StringIO())


def test_stored_secrets_without_keys_fail_the_checks(provider_table, monkeypatch):
    from skeleton.system import checks
    from skeleton.system.checks import stored_secret_key_checks, stored_secrets_without_keys

    # The stand-in is a TEMP table (pg_temp), outside the skeleton schema that physical_tables lists.
    listed = checks.physical_tables
    monkeypatch.setattr(checks, "physical_tables", lambda connection: [*listed(connection), "sk_test_providersecret"])

    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[OLD]):
        provider_table.objects.create(name="a", client_secret="secret-a")
        assert stored_secrets_without_keys(tables={provider_table._meta.db_table}) == []
    with override_settings(IDENTITY_PROVIDER_ENCRYPTION_KEYS=[]):
        problems = stored_secrets_without_keys(tables={provider_table._meta.db_table})
        assert problems and "IDENTITY_PROVIDER_ENCRYPTION_KEYS is empty" in problems[0]
        assert [error.id for error in stored_secret_key_checks(databases=["default"])] == ["skeleton.E027"]
