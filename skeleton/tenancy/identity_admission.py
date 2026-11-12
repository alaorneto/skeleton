"""The organization's admission rules for its own identity providers (#31).

The platform registers the provider (#30); the organization only adjusts who may come in
through it: JIT provisioning, approval instead of direct entry, the default role and the
allowed email domains. It reads a filtered, non-sensitive projection of the global provider
rows bound to it and writes only ``IdentityProviderAdmission``, with CAS from revision 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context

FIELDS = ("jit_provisioning", "jit_requires_approval", "default_role", "allowed_email_domains")
MAX_DOMAINS = 50


class ProviderNotFound(Exception):
    """Neutral: not a provider of this organization."""


@dataclass(frozen=True)
class ProviderStatus:
    """What the organization may see of its provider: never secrets or protocol settings."""

    sysid: UUID
    provider_id: str
    name: str
    protocol: str
    enabled: bool
    show_on_login: bool
    login_path: str
    last_check_at: Any
    last_check_code: str
    admission: Any  # IdentityProviderAdmission or None
    revision: int  # The admission revision; 0 before the first write.


def _login_path(row: Any) -> str:
    from django.urls import NoReverseMatch, reverse

    try:
        if row.protocol == "oidc":
            return reverse("openid_connect_login", kwargs={"provider_id": row.provider_id})
        if row.protocol == "saml":
            return reverse("saml_login", kwargs={"organization_slug": row.client_id})
        return reverse("ldap_login", kwargs={"provider_id": row.provider_id})
    except NoReverseMatch:
        return ""


def providers() -> list[ProviderStatus]:
    """The providers bound to the context organization, with its admission rows."""
    from skeleton.accounts.models import IdentityProvider

    from .models import IdentityProviderAdmission

    organization_id = current_context().organization_id
    rows = list(IdentityProvider.objects.filter(bound_organization_id=organization_id).order_by("provider_id"))
    admissions = {
        row.provider_id: row
        for row in IdentityProviderAdmission.objects.select_related("default_role").filter(
            provider_id__in=[item.pk for item in rows]
        )
    }
    return [
        ProviderStatus(
            sysid=row.pk,
            provider_id=row.provider_id,
            name=row.name,
            protocol=row.protocol,
            enabled=row.enabled,
            show_on_login=row.show_on_login,
            login_path=_login_path(row),
            last_check_at=row.last_check_at,
            last_check_code=row.last_check_code,
            admission=admissions.get(row.pk),
            revision=admissions[row.pk].revision if row.pk in admissions else 0,
        )
        for row in rows
    ]


def provider(sysid: Any) -> ProviderStatus:
    for item in providers():
        if str(item.sysid) == str(sysid):
            return item
    raise ProviderNotFound


def _domains(value: Any) -> list[str]:
    from .domains import normalize_domain

    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [item for item in value.replace(",", "\n").splitlines() if item.strip()]
    if not isinstance(value, list) or len(value) > MAX_DOMAINS:
        raise ValidationError({"allowed_email_domains": f"Use até {MAX_DOMAINS} domínios."})
    result = []
    for item in value:
        try:
            domain = normalize_domain(item)
        except ValidationError:
            raise ValidationError({"allowed_email_domains": f"Domínio inválido: {item}."}) from None
        if domain not in result:
            result.append(domain)
    return result


def configure(*, actor: Any, provider_sysid: Any, data: dict[str, Any], expected_revision: Any) -> Any:
    """Replace the rules of one provider with CAS; the role is never taken from a claim."""
    from skeleton.access.capabilities import IDENTITY_CONFIGURE
    from skeleton.access.models import Role
    from skeleton.access.services import organization_lock, require_unprivileged_target
    from skeleton.accounts.models import IdentityProvider

    from .models import IdentityProviderAdmission

    unknown = set(data) - set(FIELDS)
    if unknown:
        raise ValidationError({name: "Campo não permitido." for name in sorted(unknown)})
    if type(expected_revision) is not int:
        raise ValidationError({"revision": "Informe a revisão."})
    values: dict[str, Any] = {}
    for flag in ("jit_provisioning", "jit_requires_approval"):
        value = data.get(flag, False)
        if type(value) is not bool:
            raise ValidationError({flag: "Use true ou false."})
        values[flag] = value
    values["allowed_email_domains"] = _domains(data.get("allowed_email_domains"))
    with organization_lock(actor, capability=IDENTITY_CONFIGURE) as organization:
        row = IdentityProvider.objects.filter(pk=provider_sysid, bound_organization=organization).first()
        if row is None:
            raise ProviderNotFound
        role = data.get("default_role")
        if role not in (None, ""):
            if not isinstance(role, Role):
                try:
                    role = Role.objects.filter(pk=UUID(str(role))).first()
                except ValueError:
                    role = None
            if role is None or role.organization_id != organization.pk or role.deleted_at is not None:
                raise ValidationError({"default_role": "Escolha um papel ativo desta organização."})
            require_unprivileged_target(actor, role=role)
        values["default_role"] = role or None
        record = IdentityProviderAdmission.objects.select_for_update().filter(provider=row).first()
        current = record.revision if record is not None else 0
        if current != expected_revision:
            raise ConcurrentUpdate("Identity provider admission changed.")
        if record is None:
            record = IdentityProviderAdmission(organization=organization, provider=row, **values)
            record.save()
            return record
        changed = {name for name, value in values.items() if getattr(record, name) != value}
        for name in changed:
            setattr(record, name, values[name])
        if changed:
            record.save(update_fields=changed)
        return record
