"""Encryption checks: encrypted columns are sensitive and configured keyrings are valid."""

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, register
from django.db import connections

from ..synchronization import physical_tables

KEYRINGS = (
    ("MFA_ENCRYPTION_KEYS", "MFA"),
    ("IDENTITY_PROVIDER_ENCRYPTION_KEYS", "identity provider"),
    ("INTEGRATION_ENCRYPTION_KEYS", "integration"),
    ("BILLING_ENCRYPTION_KEYS", "billing"),
)


@register()
def encrypted_secret_checks(app_configs=None, **kwargs):
    """Encrypted columns are audited as sensitive and configured keyrings are valid Fernet keys."""
    from django.core.exceptions import ImproperlyConfigured

    from skeleton.common.crypto import EncryptedTextField, validate_keyring
    from skeleton.system.registry import get_spec

    errors = []
    for setting, purpose in KEYRINGS:
        try:
            validate_keyring(setting, purpose=purpose)
        except ImproperlyConfigured as exc:
            errors.append(Error(str(exc), id="skeleton.E026"))
    for model in kwargs.get("apps_registry", apps).get_models():
        for field in model._meta.concrete_fields:
            if not isinstance(field, EncryptedTextField):
                continue
            try:
                sensitive = set(get_spec(model._meta.label_lower).sensitive_fields)
            except KeyError:
                continue  # E002 already reports the missing TableSpec.
            if field.name not in sensitive:
                errors.append(
                    Error(
                        f"{model._meta.label}.{field.name}: encrypted secrets must be in TableSpec.sensitive_fields.",
                        id="skeleton.E026",
                    )
                )
    return errors


def stored_secrets_without_keys(*, using="default", tables=None):
    """Messages for keyrings that are empty while encrypted values are stored (fails closed)."""
    from skeleton.common.crypto import encrypted_fields

    if tables is None:
        tables = set(physical_tables(connections[using]))
    problems = []
    for setting, _ in KEYRINGS:
        if getattr(settings, setting, ()):
            continue
        for model, field in encrypted_fields(setting):
            if model._meta.db_table not in tables:
                continue  # Not migrated yet: nothing stored.
            if model._base_manager.using(using).filter(**{f"{field.attname}__isnull": False}).exists():
                problems.append(f"{model._meta.label}.{field.name} stores secrets but {setting} is empty.")
    return problems


@register("database")
def stored_secret_key_checks(app_configs=None, databases=None, **kwargs):
    errors = []
    for alias in databases or ():
        errors.extend(Error(message, id="skeleton.E027") for message in stored_secrets_without_keys(using=alias))
    return errors
