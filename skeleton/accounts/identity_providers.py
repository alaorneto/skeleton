"""Identity providers stored in the database (#29), next to the private JSON file.

The platform registers OIDC and SAML providers (LDAP in #32) per organization without a
restart. A row becomes, in memory and per request, the same ``ProviderBinding`` and allauth
``SocialApp`` the JSON produced: the binding fingerprint is the same v2 tuple, so a provider
imported from the file keeps every ``SocialAccount`` valid. The identity (provider id,
protocol, organization, issuer/entity/directory, client id, SP entity and fingerprint) never
changes: the table guard and ``update_identity_provider`` refuse it. Secrets are written
encrypted and never read back into forms, logs or audit.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from skeleton.common.exceptions import ConcurrentUpdate

PROTOCOLS = ("oidc", "saml", "ldap")
IDENTITY_FIELDS = ("provider_id", "protocol", "bound_organization", "issuer", "entity_id", "directory", "client_id")
MUTABLE_FIELDS = ("name", "enabled", "show_on_login", "trust_verified_email", "settings", "secret", "private_key")
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# Protocol options a row may hold; never secrets, never email proof, scopes or protocol switches.
SAML_IDP_KEYS = {"sso_url", "slo_url", "metadata_url", "x509cert"}
SAML_SP_KEYS = {"x509cert"}
SAML_MAPPING_KEYS = {"uid", "email", "email_verified", "first_name", "last_name"}
LDAP_KEYS: set[str] = set()  # #32 declares the directory options.
SAML_ADVANCED = {
    "strict": True,
    "want_assertion_signed": True,
    "want_message_signed": True,
    "reject_deprecated_algorithm": True,
    "reject_idp_initiated_sso": True,
}


def binding_fingerprint(
    protocol: str, identity: str, client_id: str, organization_slug: str, sp_entity_id: str = ""
) -> str:
    """The v2 binding fingerprint, shared with the JSON loader (``providers.load_provider_config``)."""
    identity_tuple = [
        "skeleton-binding-v2",
        protocol,
        identity,
        client_id,
        organization_slug or "",
        sp_entity_id if protocol == "saml" else "",
    ]
    return hashlib.sha256(json.dumps(identity_tuple, separators=(",", ":")).encode()).hexdigest()


def _https(value: Any, field: str) -> str:
    from urllib.parse import urlparse

    from django.conf import settings

    if not isinstance(value, str) or not value:
        raise ValidationError({field: "Informe uma URL."})
    parsed = urlparse(value)
    loopback = settings.DEBUG and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.username or parsed.password or not parsed.hostname or (parsed.scheme != "https" and not loopback):
        raise ValidationError({field: "Use uma URL HTTPS sem credenciais."})
    return value


def normalize_settings(protocol: str, value: Any) -> dict[str, Any]:
    """The allowlisted, non-secret options of ``protocol``; anything else is refused."""
    if value in (None, ""):
        value = {}
    if not isinstance(value, dict):
        raise ValidationError({"settings": "Use um objeto."})
    if protocol == "oidc":
        if value:
            raise ValidationError({"settings": "OIDC não aceita opções: escopos e prova de email são do código."})
        return {}
    if protocol == "ldap":
        unknown = set(value) - LDAP_KEYS
        if unknown:
            raise ValidationError({"settings": f"Opções não permitidas: {', '.join(sorted(unknown))}."})
        return deepcopy(value)
    unknown = set(value) - {"idp", "sp", "attribute_mapping"}
    if unknown:
        raise ValidationError({"settings": f"Opções não permitidas: {', '.join(sorted(unknown))}."})
    idp = value.get("idp", {})
    sp = value.get("sp", {})
    mapping = value.get("attribute_mapping", {})
    if not isinstance(idp, dict) or set(idp) - SAML_IDP_KEYS:
        raise ValidationError({"settings": "idp aceita sso_url, slo_url, metadata_url e x509cert."})
    if not isinstance(sp, dict) or set(sp) - SAML_SP_KEYS:
        raise ValidationError({"settings": "sp aceita só x509cert (a chave privada é um segredo à parte)."})
    if not isinstance(mapping, dict) or set(mapping) - SAML_MAPPING_KEYS:
        raise ValidationError({"settings": "attribute_mapping aceita uid, email, email_verified e nomes."})
    for key in ("sso_url", "slo_url", "metadata_url"):
        if idp.get(key):
            _https(idp[key], "settings")
    if not idp.get("x509cert") and not idp.get("metadata_url"):
        raise ValidationError({"settings": "SAML exige o certificado ou a URL de metadata do IdP."})
    return {"idp": dict(idp), "sp": dict(sp), "attribute_mapping": dict(mapping)}


def _require_platform(actor: Any) -> None:
    from skeleton.accounts.platform import is_platform_admin

    if actor is not None and not is_platform_admin(actor):
        raise PermissionDenied("Platform administration required.")


def create_identity_provider(*, actor: Any, data: dict[str, Any]) -> Any:
    """Create a provider. ``actor=None`` is the offline import (CLI, its own system context)."""
    from .models import IdentityProvider

    _require_platform(actor)
    allowed = {*IDENTITY_FIELDS, "sp_entity_id", *MUTABLE_FIELDS}
    unknown = set(data) - allowed
    if unknown:
        raise ValidationError({name: "Campo não permitido." for name in sorted(unknown)})
    protocol = data.get("protocol")
    if protocol not in PROTOCOLS:
        raise ValidationError({"protocol": "Use oidc, saml ou ldap."})
    provider_id = data.get("provider_id")
    if not isinstance(provider_id, str) or not _PROVIDER_ID.match(provider_id):
        raise ValidationError({"provider_id": "Use letras minúsculas, números, - e _."})
    organization = data.get("bound_organization")
    if organization is None or not organization.is_active or organization.deleted_at is not None:
        raise ValidationError({"bound_organization": "Escolha uma organização ativa."})
    from .providers import configured_file_providers

    file_binding = configured_file_providers().get(provider_id)
    values = {
        "provider_id": provider_id,
        "protocol": protocol,
        "bound_organization": organization,
        "issuer": "",
        "entity_id": "",
        "directory": "",
        "client_id": data.get("client_id") or "",
        "sp_entity_id": data.get("sp_entity_id") or "",
        "name": data.get("name") or provider_id,
        "settings": normalize_settings(protocol, data.get("settings")),
    }
    if protocol == "oidc":
        values["issuer"] = _https(data.get("issuer"), "issuer").rstrip("/")
        identity = values["issuer"]
        if not values["client_id"] or not data.get("secret"):
            raise ValidationError({"client_id": "OIDC exige client_id e secret."})
    elif protocol == "saml":
        values["entity_id"] = data.get("entity_id") or ""
        identity = values["entity_id"]
        if not identity or not values["client_id"]:
            raise ValidationError({"entity_id": "SAML exige o entity_id do IdP e o client_id (slug da URL)."})
    else:
        values["directory"] = data.get("directory") or ""
        identity = values["directory"]
        if not identity:
            raise ValidationError({"directory": "LDAP exige o diretório."})
    values["fingerprint"] = binding_fingerprint(
        protocol, identity, values["client_id"], organization.slug, values["sp_entity_id"]
    )
    if file_binding is not None and file_binding.fingerprint != values["fingerprint"]:
        raise ValidationError({"provider_id": "O arquivo de provedores já usa este provider_id com outra identidade."})
    for flag in ("enabled", "show_on_login", "trust_verified_email"):
        if flag in data:
            if type(data[flag]) is not bool:
                raise ValidationError({flag: "Use true ou false."})
            values[flag] = data[flag]
    record = IdentityProvider(**values)
    for secret in ("secret", "private_key"):
        value = data.get(secret)
        if value not in (None, ""):
            if not isinstance(value, str):
                raise ValidationError({secret: "Use um texto."})
            setattr(record, secret, value)
    with transaction.atomic():
        if IdentityProvider.all_objects.filter(provider_id=provider_id).exists():
            raise ValidationError({"provider_id": ValidationError("Este provider_id já existe.", code="unique")})
        record.save()
    forget_database_bindings()
    return record


def update_identity_provider(*, actor: Any, provider_id: Any, data: dict[str, Any], expected_revision: Any) -> Any:
    """Change mutable fields with CAS; an empty secret keeps the stored one."""
    from .models import IdentityProvider

    _require_platform(actor)
    unknown = set(data) - set(MUTABLE_FIELDS)
    if unknown:
        raise ValidationError({name: "Campo imutável ou não permitido." for name in sorted(unknown)})
    with transaction.atomic():
        record = IdentityProvider.objects.select_for_update().filter(pk=provider_id).first()
        if record is None:
            raise ValidationError({"provider_id": "Provedor inexistente."})
        if type(expected_revision) is not int or record.revision != expected_revision:
            raise ConcurrentUpdate("Identity provider changed.")
        changed = set()
        for flag in ("enabled", "show_on_login", "trust_verified_email"):
            if flag in data:
                if type(data[flag]) is not bool:
                    raise ValidationError({flag: "Use true ou false."})
                if getattr(record, flag) != data[flag]:
                    setattr(record, flag, data[flag])
                    changed.add(flag)
        if "name" in data:
            name = data["name"]
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
                raise ValidationError({"name": "Use um nome de 1 a 200 caracteres."})
            if record.name != name.strip():
                record.name = name.strip()
                changed.add("name")
        if "settings" in data:
            options = normalize_settings(record.protocol, data["settings"])
            if options != record.settings:
                record.settings = options
                changed.add("settings")
        for secret in ("secret", "private_key"):
            value = data.get(secret)
            if value not in (None, ""):
                if not isinstance(value, str):
                    raise ValidationError({secret: "Use um texto."})
                setattr(record, secret, value)
                changed.add(secret)
        if changed:
            record.save(update_fields=changed)
    forget_database_bindings()
    return record


# --------------------------------------------------------------------------- runtime bindings

_MEMO = "skeleton.accounts.database_bindings"


def forget_database_bindings() -> None:
    from skeleton.tenancy.context import request_cache

    cache = request_cache()
    if cache is not None:
        cache.pop(_MEMO, None)


def _reveal(value: Any) -> str:
    return value.reveal() if value is not None else ""


def binding_from_row(row: Any) -> Any:
    """The in-memory ``ProviderBinding`` (and allauth app) of one enabled row."""
    from .providers import ProviderBinding

    settings: dict[str, Any] = {}
    app: dict[str, Any] = {"provider_id": row.provider_id, "name": row.name, "client_id": row.client_id}
    if row.protocol == "oidc":
        app["secret"] = _reveal(row.secret)
        settings = {
            "server_url": row.issuer,
            "uid_field": "sub",
            "oauth_pkce_enabled": True,
            "token_auth_method": "client_secret_basic",
        }
    elif row.protocol == "saml":
        options = row.settings or {}
        advanced = dict(SAML_ADVANCED)
        if row.private_key is not None:
            advanced["private_key"] = _reveal(row.private_key)
        if options.get("sp", {}).get("x509cert"):
            advanced["x509cert"] = options["sp"]["x509cert"]
        settings = {
            "idp": {"entity_id": row.entity_id, **options.get("idp", {})},
            "sp": {"entity_id": row.sp_entity_id} if row.sp_entity_id else {},
            "advanced": advanced,
        }
        if options.get("attribute_mapping"):
            settings["attribute_mapping"] = dict(options["attribute_mapping"])
    if not row.show_on_login:
        settings["hidden"] = True
    app["settings"] = settings
    return ProviderBinding(
        protocol=row.protocol,
        provider_id=row.provider_id,
        organization_slug=row.bound_organization.slug,
        jit_provisioning=False,  # The organization decides in IdentityProviderAdmission.
        default_role_code="",
        trust_verified_email=row.trust_verified_email,
        fingerprint=row.fingerprint,
        app=app,
        source="db",
        sysid=row.pk,
    )


def database_bindings() -> tuple[Any, ...]:
    """Enabled providers of active organizations, read once per request (never across requests)."""
    from skeleton.tenancy.context import request_cache

    from .models import IdentityProvider

    cache = request_cache()
    if cache is not None and _MEMO in cache:
        return cache[_MEMO]
    rows = IdentityProvider.objects.filter(
        enabled=True,
        protocol__in=("oidc", "saml"),
        bound_organization__is_active=True,
        bound_organization__deleted_at__isnull=True,
    ).select_related("bound_organization")
    result = tuple(binding_from_row(row) for row in rows.order_by("provider_id"))
    if cache is not None:
        cache[_MEMO] = result
    return result


# --------------------------------------------------------------------------- connection test (#30)


def probe(row: Any) -> str:
    """Fetch OIDC discovery or SAML metadata with the outbound client's timeouts; no login, no write."""
    from skeleton.common.outbound_http import OutboundRequestFailed, OutboundRequestRefused, request

    if row.protocol == "oidc":
        url = row.issuer.rstrip("/") + "/.well-known/openid-configuration"
    elif row.protocol == "saml":
        url = (row.settings or {}).get("idp", {}).get("metadata_url")
        if not url:
            return "no_metadata_url"
    else:
        return "unsupported"
    try:
        response = request("GET", url, headers={"Accept": "application/json, application/xml"})
    except OutboundRequestRefused as exc:
        return f"refused:{exc.code}"
    except OutboundRequestFailed:
        return "unreachable"
    if response.status != 200:
        return f"http_{response.status}"
    if row.protocol == "oidc":
        try:
            document = json.loads(response.body)
        except ValueError:
            return "invalid_document"
        issuer = document.get("issuer") if isinstance(document, dict) else None
        return "ok" if isinstance(issuer, str) and issuer.rstrip("/") == row.issuer.rstrip("/") else "issuer_mismatch"
    return "ok" if row.entity_id.encode() in response.body else "entity_mismatch"


def check_identity_provider(*, actor: Any, provider_id: Any) -> str:
    """Run ``probe`` outside any lock and record only ``last_check_at``/``last_check_code``."""
    from django.utils import timezone

    from .models import IdentityProvider

    _require_platform(actor)
    row = IdentityProvider.objects.filter(pk=provider_id).first()
    if row is None:
        raise ValidationError({"provider_id": "Provedor inexistente."})
    code = probe(row)
    with transaction.atomic():
        locked = IdentityProvider.objects.select_for_update().get(pk=row.pk)
        locked.last_check_at = timezone.now()
        locked.last_check_code = code[:40]
        locked.save(update_fields={"last_check_at", "last_check_code"})
    return code


def organization_providers(organization: Any) -> list[Any]:
    """The providers bound to one organization: global rows, never its tenant data."""
    from .models import IdentityProvider

    return list(IdentityProvider.objects.filter(bound_organization=organization).order_by("provider_id"))
