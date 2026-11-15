"""Custom domains of an organization address (#488, #489), proven by a DNS TXT record.

The administrator adds ``portal.cliente.com.br`` and publishes the challenge in
``_skeleton-site.<domain>``. Verification resolves DNS (3 s timeout) before taking any lock,
then locks the organization and the row. A domain already proven by another organization
fails with a neutral error. The global ``SiteDomainClaim`` is written only here, in the
``SiteDomain`` transaction. Email domains (``skeleton.tenancy.domains``) are a different
table and challenge: proving one never authorizes the other.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

from .domains import MAX_FAILURES, DomainUnavailable, RecheckReport, Resolver, _check, normalize_domain

SITE_CHALLENGE_PREFIX = "_skeleton-site."
SITE_RECHECK_REASON = "site-domain:recheck"
MAX_SITE_DOMAINS = 3
CUSTOM_HOSTS_TTL_S = 30.0
CUSTOM_HOSTS_STALE_S = 300.0
logger = logging.getLogger("skeleton.tenancy")

__all__ = [
    "MAX_SITE_DOMAINS",
    "SITE_CHALLENGE_PREFIX",
    "DomainUnavailable",
    "add_site_domain",
    "custom_domains_enabled",
    "custom_hosts",
    "recheck_site_domains",
    "reset_custom_hosts_cache",
    "revoke_site_claim",
    "revoke_site_domain",
    "site_claims",
    "site_domains",
    "verify_site_domain",
]


def _models() -> Any:
    from . import models

    return models


def custom_domains_enabled() -> bool:
    """``SKELETON_ORGANIZATION_CUSTOM_DOMAINS``, which only counts with a base domain."""
    from .site import base_domain

    return bool(getattr(settings, "SKELETON_ORGANIZATION_CUSTOM_DOMAINS", False)) and bool(base_domain())


# --------------------------------------------------------------------------- organization


def site_domains() -> Any:
    """The custom domains of the context organization (RLS)."""
    return _models().SiteDomain.objects.order_by("domain")


def _refusal(domain: str) -> str | None:
    from django.http.request import validate_host

    from .site import _static_allowed_hosts, base_domain, platform_hosts

    base = base_domain()
    if domain == base or domain.endswith("." + base):
        return gettext("Use um domínio fora de %(base)s: os endereços nele já existem.") % {"base": base}
    if domain in platform_hosts() or validate_host(domain, _static_allowed_hosts()):
        return gettext("Este domínio é da própria instalação.")
    return None


def add_site_domain(*, actor: Any, domain: Any) -> Any:
    """Register a custom domain of the administrator's organization; the challenge is generated here."""
    from skeleton.access.services import organization_lock

    models = _models()
    if not custom_domains_enabled():
        raise ValidationError({"domain": gettext("Esta instalação não aceita domínios próprios.")})
    domain = normalize_domain(domain)
    refusal = _refusal(domain)
    if refusal:
        raise ValidationError({"domain": refusal})
    with organization_lock(actor) as organization:
        rows = models.SiteDomain.objects.filter(organization=organization)
        if rows.filter(domain=domain).exists():
            raise ValidationError({"domain": ValidationError(gettext("Domínio já cadastrado."), code="unique")})
        if rows.count() >= MAX_SITE_DOMAINS:
            raise ValidationError({"domain": gettext("Uma organização tem no máximo 3 domínios próprios.")})
        record = models.SiteDomain(
            organization=organization, domain=domain, challenge="skeleton-site=" + secrets.token_hex(16)
        )
        record.save()
        return record


def verify_site_domain(*, actor: Any, domain_id: Any, resolver: Resolver | None = None) -> Any:
    """Resolve first, then lock; a domain proven by another organization is a neutral failure."""
    from skeleton.access.services import organization_lock

    models = _models()
    found = models.SiteDomain.objects.filter(pk=domain_id).values_list("domain", "challenge").first()
    if found is None:
        raise DomainUnavailable
    error = _check(found[0], found[1], resolver, prefix=SITE_CHALLENGE_PREFIX)
    with organization_lock(actor) as organization:
        record = models.SiteDomain.objects.select_for_update().filter(pk=domain_id).first()
        if record is None:
            raise DomainUnavailable
        now = timezone.now()
        record.last_checked_at = now
        record.last_error_code = error
        changed = {"last_checked_at", "last_error_code"}
        if not error:
            claim = models.SiteDomainClaim.objects.select_for_update().filter(domain=record.domain).first()
            if claim is not None and claim.claiming_organization_id != organization.pk:
                record.last_error_code = "unavailable"
                record.save(update_fields=changed)
                raise DomainUnavailable
            if claim is None:
                models.SiteDomainClaim(domain=record.domain, claiming_organization=organization).save()
            record.status = models.SiteDomain.Status.VERIFIED
            record.verified_at = record.verified_at or now
            record.failure_count = 0
            changed |= {"status", "verified_at", "failure_count"}
        record.save(update_fields=changed)
        return record


def _drop_claim(domain: str, organization_id: UUID) -> None:
    claim = (
        _models()
        .SiteDomainClaim.objects.select_for_update()
        .filter(domain=domain, claiming_organization_id=organization_id)
        .first()
    )
    if claim is not None:
        claim.delete()


def revoke_site_domain(*, actor: Any, domain_id: Any, expected_revision: Any) -> Any:
    """Remove the domain (soft delete) and its claim in the same transaction, with CAS."""
    from skeleton.access.services import organization_lock

    models = _models()
    with organization_lock(actor) as organization:
        record = models.SiteDomain.objects.select_for_update().filter(pk=domain_id).first()
        if record is None:
            raise DomainUnavailable
        if type(expected_revision) is not int or record.revision != expected_revision:
            raise ConcurrentUpdate("Site domain changed.")
        _drop_claim(record.domain, organization.pk)
        record.delete()
        return record


# --------------------------------------------------------------------------- platform


def site_claims() -> Any:
    """Every live custom domain claim, for the platform; global rows only."""
    return _models().SiteDomainClaim.objects.select_related("claiming_organization").order_by("domain")


def revoke_site_claim(*, actor: Any, claim_id: Any, expected_revision: Any) -> Any:
    """The platform drops a claim (dispute, abuse); the organization's row is untouched."""
    from skeleton.accounts.platform import require_platform_admin

    require_platform_admin(actor)
    with transaction.atomic():
        claim = _models().SiteDomainClaim.objects.select_for_update().filter(pk=claim_id).first()
        if claim is None:
            raise DomainUnavailable
        if type(expected_revision) is not int or claim.revision != expected_revision:
            raise ConcurrentUpdate("Site domain claim changed.")
        claim.delete()
        return claim


# --------------------------------------------------------------------------- recheck


def recheck_site_domains(*, resolver: Resolver | None = None, report: RecheckReport | None = None) -> RecheckReport:
    """Re-resolve every claimed custom domain; one transaction per organization, DNS outside them."""
    from .context import system_context

    models = _models()
    report = report or RecheckReport()
    by_organization: dict[UUID, list[str]] = {}
    for organization_id, domain in models.SiteDomainClaim.objects.values_list("claiming_organization_id", "domain"):
        by_organization.setdefault(organization_id, []).append(domain)
    for organization_id, names in by_organization.items():
        with system_context(reason=SITE_RECHECK_REASON, organization_id=organization_id):
            rows = list(
                models.SiteDomain.objects.filter(
                    domain__in=names, status=models.SiteDomain.Status.VERIFIED
                ).values_list("pk", "domain", "challenge")
            )
        results = {
            pk: _check(domain, challenge, resolver, prefix=SITE_CHALLENGE_PREFIX) for pk, domain, challenge in rows
        }
        with system_context(reason=SITE_RECHECK_REASON, organization_id=organization_id), transaction.atomic():
            for pk, error in results.items():
                record = models.SiteDomain.objects.select_for_update().filter(pk=pk).first()
                if record is None or record.status != models.SiteDomain.Status.VERIFIED:
                    continue
                report.checked += 1
                record.last_checked_at = timezone.now()
                record.last_error_code = error
                record.failure_count = 0 if not error else record.failure_count + 1
                changed = {"last_checked_at", "last_error_code", "failure_count"}
                if error:
                    report.failures += 1
                if record.failure_count >= MAX_FAILURES:
                    record.status = models.SiteDomain.Status.FAILED
                    changed.add("status")
                    _drop_claim(record.domain, organization_id)
                    report.revoked += 1
                record.save(update_fields=changed)
    return report


# --------------------------------------------------------------------------- hosts (#489)

_CACHE: dict[str, Any] = {"value": None, "at": 0.0, "good": None, "good_at": 0.0}


def reset_custom_hosts_cache() -> None:
    """Forget the cached custom hosts (tests, and right after a claim changes in this process)."""
    _CACHE.update(value=None, at=0.0, good=None, good_at=0.0)


def custom_hosts() -> dict[str, UUID]:
    """Verified custom domains -> organization id; cached per process for ``CUSTOM_HOSTS_TTL_S``.

    Empty without the feature, without the base domain or before the apps are ready (the
    settings are imported first). A database error keeps the last good value for five minutes,
    then fails closed with no custom host at all.
    """
    from django.apps import apps

    if not custom_domains_enabled() or not apps.ready:
        return {}
    now = time.monotonic()
    if _CACHE["value"] is not None and now - _CACHE["at"] < CUSTOM_HOSTS_TTL_S:
        return _CACHE["value"]
    from django.db import DatabaseError

    try:
        rows = (
            _models()
            .SiteDomainClaim.objects.filter(
                claiming_organization__is_active=True, claiming_organization__deleted_at__isnull=True
            )
            .values_list("domain", "claiming_organization_id")
        )
        value = {domain: organization_id for domain, organization_id in rows}
    except DatabaseError:
        logger.warning("site.custom_hosts_unavailable")  # Never the host: no domain in logs.
        if _CACHE["good"] is not None and now - _CACHE["good_at"] < CUSTOM_HOSTS_STALE_S:
            return _CACHE["good"]
        return {}
    _CACHE.update(value=value, at=now, good=value, good_at=now)
    return value
