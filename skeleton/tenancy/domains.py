"""Admission by verified email domain (#28).

The organization adds a domain and publishes the challenge in a TXT record at
``_skeleton-challenge.<domain>``. Verification resolves DNS (3 s timeout) before taking any
lock, then locks the organization and the domain row. A domain already claimed by another
organization fails with a neutral error. The global ``DomainClaim`` is the index read before
any tenant exists; it is written only here, in the ``VerifiedDomain`` transaction.

``apply_verified_email_admission`` runs when an email is confirmed (and for verified social
emails): the claim names at most one organization, so an email of that domain never leads to
another one. Without a previous membership it admits (``auto``) or asks (``approval``); a
former member always gets a request. ``verify_domains --recheck`` re-resolves verified domains;
three failures in a row mark the domain ``failed`` and drop the claim.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import system_context

DOMAIN_REASON = "join:domain"
RECHECK_REASON = "domain:recheck"
CHALLENGE_PREFIX = "_skeleton-challenge."
DNS_TIMEOUT_S = 3.0
MAX_FAILURES = 3
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

# resolver(name) -> TXT strings; raises LookupError(code) when the lookup itself failed.
Resolver = Callable[[str], Iterable[str]]


class DomainUnavailable(Exception):
    """Neutral: the domain cannot be verified by this organization."""


def _models() -> Any:
    from . import models

    return models


def normalize_domain(value: Any) -> str:
    """Lowercase IDNA (A-label) form with at least two valid labels; ``ValidationError`` otherwise."""
    if not isinstance(value, str):
        raise ValidationError({"domain": "Informe um domínio."})
    value = value.strip().rstrip(".").lower()
    if value.startswith("*.") or "@" in value or "/" in value:
        raise ValidationError({"domain": "Informe só o domínio, como empresa.com.br."})
    try:
        ascii_value = value.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValidationError({"domain": "Domínio inválido."}) from None
    labels = ascii_value.split(".")
    if len(ascii_value) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise ValidationError({"domain": "Domínio inválido."})
    if labels[-1].isdigit():
        raise ValidationError({"domain": "Domínio inválido."})
    return ascii_value


def email_domain(email: str) -> str | None:
    try:
        return normalize_domain(email.rsplit("@", 1)[1])
    except (IndexError, ValidationError):
        return None


def dns_txt(name: str) -> list[str]:
    """TXT strings of ``name`` with a hard timeout; ``LookupError(code)`` on failure."""
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT_S
    resolver.timeout = DNS_TIMEOUT_S
    try:
        answer = resolver.resolve(name, "TXT")
    except dns.resolver.NXDOMAIN:
        raise LookupError("nxdomain") from None
    except dns.resolver.NoAnswer:
        return []
    except dns.exception.Timeout:
        raise LookupError("timeout") from None
    except dns.exception.DNSException:
        raise LookupError("dns_error") from None
    return [b"".join(record.strings).decode("utf-8", "replace") for record in answer]


def _check(domain: str, challenge: str, resolver: Resolver | None) -> str:
    """``""`` when the challenge is published; an error code otherwise. Never inside a lock."""
    try:
        records = list((resolver or dns_txt)(CHALLENGE_PREFIX + domain))
    except LookupError as error:
        return str(error.args[0]) if error.args else "dns_error"
    return "" if challenge in (record.strip() for record in records) else "challenge_missing"


# --------------------------------------------------------------------------- organization


def domains() -> Any:
    return _models().VerifiedDomain.objects.select_related("role").order_by("domain")


def add_domain(*, actor: Any, domain: Any, admission: Any, role: Any) -> Any:
    from skeleton.access.capabilities import ADMISSION_CONFIGURE
    from skeleton.access.services import organization_lock

    models = _models()
    domain = normalize_domain(domain)
    if admission not in models.VerifiedDomain.Admission.values:
        raise ValidationError({"admission": "Use auto ou approval."})
    with organization_lock(actor, capability=ADMISSION_CONFIGURE) as organization:
        if role is None or role.organization_id != organization.pk or role.deleted_at is not None:
            raise ValidationError({"role": "Escolha um papel ativo desta organização."})
        if role.is_builtin and role.code == "organization_admin":
            raise ValidationError({"role": "O papel de administrador não é concedido por domínio."})
        if models.VerifiedDomain.objects.filter(domain=domain).exists():
            raise ValidationError({"domain": ValidationError("Domínio já cadastrado.", code="unique")})
        record = models.VerifiedDomain(
            organization=organization,
            domain=domain,
            challenge="skeleton-verify=" + secrets.token_hex(16),
            admission=admission,
            role=role,
        )
        record.save()
        return record


def verify_domain(*, actor: Any, domain_id: Any, resolver: Resolver | None = None) -> Any:
    """Resolve first, then lock; a domain claimed by another organization is a neutral failure."""
    from skeleton.access.capabilities import ADMISSION_CONFIGURE
    from skeleton.access.services import organization_lock

    models = _models()
    found = models.VerifiedDomain.objects.filter(pk=domain_id).values_list("domain", "challenge").first()
    if found is None:
        raise DomainUnavailable
    error = _check(found[0], found[1], resolver)
    with organization_lock(actor, capability=ADMISSION_CONFIGURE) as organization:
        record = models.VerifiedDomain.objects.select_for_update().filter(pk=domain_id).first()
        if record is None:
            raise DomainUnavailable
        now = timezone.now()
        record.last_checked_at = now
        record.last_error_code = error
        changed = {"last_checked_at", "last_error_code"}
        if not error:
            claim = models.DomainClaim.objects.select_for_update().filter(domain=record.domain).first()
            if claim is not None and claim.claiming_organization_id != organization.pk:
                record.last_error_code = "unavailable"
                record.save(update_fields=changed)
                raise DomainUnavailable
            if claim is None:
                models.DomainClaim(domain=record.domain, claiming_organization=organization).save()
            record.status = models.VerifiedDomain.Status.VERIFIED
            record.verified_at = record.verified_at or now
            record.failure_count = 0
            changed |= {"status", "verified_at", "failure_count"}
        record.save(update_fields=changed)
        return record


def revoke_domain(*, actor: Any, domain_id: Any, expected_revision: Any) -> Any:
    from skeleton.access.capabilities import ADMISSION_CONFIGURE
    from skeleton.access.services import organization_lock

    models = _models()
    with organization_lock(actor, capability=ADMISSION_CONFIGURE) as organization:
        record = models.VerifiedDomain.objects.select_for_update().filter(pk=domain_id).first()
        if record is None:
            raise DomainUnavailable
        if type(expected_revision) is not int or record.revision != expected_revision:
            raise ConcurrentUpdate("Verified domain changed.")
        _drop_claim(record.domain, organization.pk)
        record.delete()
        return record


def _drop_claim(domain: str, organization_id: UUID) -> None:
    claim = (
        _models()
        .DomainClaim.objects.select_for_update()
        .filter(domain=domain, claiming_organization_id=organization_id)
        .first()
    )
    if claim is not None:
        claim.delete()


# --------------------------------------------------------------------------- platform


def claims() -> Any:
    """Every live claim, for the platform; global rows only."""
    return _models().DomainClaim.objects.select_related("claiming_organization").order_by("domain")


def revoke_claim(*, actor: Any, claim_id: Any, expected_revision: Any) -> Any:
    """The platform settles a disputed domain by dropping the claim; the tenant row is untouched."""
    from skeleton.accounts.platform import require_platform_admin

    require_platform_admin(actor)
    with transaction.atomic():
        claim = _models().DomainClaim.objects.select_for_update().filter(pk=claim_id).first()
        if claim is None:
            raise DomainUnavailable
        if type(expected_revision) is not int or claim.revision != expected_revision:
            raise ConcurrentUpdate("Domain claim changed.")
        claim.delete()
        return claim


# --------------------------------------------------------------------------- admission


@dataclass(frozen=True)
class DomainSuggestion:
    organization_id: UUID
    name: str
    domain: str


def suggestions(user: Any) -> tuple[DomainSuggestion, ...]:
    """Organizations that verified the domain of one of the person's verified emails."""
    from allauth.account.models import EmailAddress

    from .context import memberships_for_user

    if not getattr(user, "is_authenticated", False):
        return ()
    domains_of_user = {
        domain
        for domain in (
            email_domain(email)
            for email in EmailAddress.objects.filter(user=user, verified=True).values_list("email", flat=True)
        )
        if domain
    }
    if not domains_of_user:
        return ()
    mine = {item["organization_id"] for item in memberships_for_user(user)}
    models = _models()
    accepting = set(
        models.OrganizationDirectoryEntry.objects.filter(accepts_domain=True).values_list(
            "listed_organization_id", flat=True
        )
    )
    rows = (
        models.DomainClaim.objects.filter(
            domain__in=domains_of_user,
            claiming_organization__is_active=True,
            claiming_organization__deleted_at__isnull=True,
        )
        .exclude(claiming_organization_id__in=mine)
        .order_by("claiming_organization__name")
        .values_list("claiming_organization_id", "claiming_organization__name", "domain")
    )
    return tuple(DomainSuggestion(pk, name, domain) for pk, name, domain in rows if pk in accepting)


def apply_verified_email_admission(user: Any, email: str) -> str:
    """Admit or ask on behalf of a confirmed ``email``; returns what happened (for tests and UI)."""
    from allauth.account.models import EmailAddress

    from skeleton.access.services import admit_member

    from .admission import REQUEST_TTL, effective_status
    from .models import Membership, Organization

    models = _models()
    if not user.is_active or user.deleted_at is not None:
        return "none"
    if not EmailAddress.objects.filter(user=user, email__iexact=email, verified=True).exists():
        return "none"
    domain = email_domain(email)
    claim = models.DomainClaim.objects.filter(domain=domain).values_list("claiming_organization_id", flat=True).first()
    if domain is None or claim is None:
        return "none"
    with system_context(reason=DOMAIN_REASON, organization_id=claim), transaction.atomic():
        organization = Organization.objects.select_for_update(no_key=True).filter(pk=claim, is_active=True).first()
        policy = models.OrganizationAdmissionPolicy.objects.filter(organization=organization).first()
        record = (
            models.VerifiedDomain.objects.select_related("role")
            .filter(domain=domain, status=models.VerifiedDomain.Status.VERIFIED)
            .first()
        )
        if organization is None or policy is None or not policy.domain_join_enabled or record is None:
            return "none"
        if Membership.objects.filter(user=user, is_active=True).exists():
            return "member"
        open_request = (
            models.JoinRequest.objects.select_for_update().filter(user=user, status__in=models.JoinRequest.OPEN).first()
        )
        if open_request is not None and effective_status(open_request) != models.JoinRequest.Status.EXPIRED:
            return "requested"
        if open_request is not None:
            open_request.status = models.JoinRequest.Status.EXPIRED
            open_request.save(update_fields={"status"})
        returning = Membership.all_objects.filter(user=user).exists()
        if record.admission == record.Admission.AUTO and not returning:
            admit_member(organization=organization, user=user, role=record.role)
            return "admitted"
        # A former member is never readmitted by a domain: a person decides.
        models.JoinRequest(
            organization=organization,
            user=user,
            channel=models.JoinRequest.Channel.DOMAIN,
            role=record.role,
            status=models.JoinRequest.Status.PENDING,
            expires_at=timezone.now() + REQUEST_TTL,
        ).save()
        return "requested"


# --------------------------------------------------------------------------- recheck


@dataclass
class RecheckReport:
    checked: int = 0
    failures: int = 0
    revoked: int = 0


def recheck(*, resolver: Resolver | None = None, report: RecheckReport | None = None) -> RecheckReport:
    """Re-resolve every claimed domain; one transaction per organization, DNS outside them."""
    models = _models()
    report = report or RecheckReport()
    targets = list(models.DomainClaim.objects.values_list("claiming_organization_id", "domain"))
    by_organization: dict[UUID, list[str]] = {}
    for organization_id, domain in targets:
        by_organization.setdefault(organization_id, []).append(domain)
    for organization_id, names in by_organization.items():
        with system_context(reason=RECHECK_REASON, organization_id=organization_id):
            rows = list(
                models.VerifiedDomain.objects.filter(
                    domain__in=names, status=models.VerifiedDomain.Status.VERIFIED
                ).values_list("pk", "domain", "challenge")
            )
        results = {pk: _check(domain, challenge, resolver) for pk, domain, challenge in rows}
        with system_context(reason=RECHECK_REASON, organization_id=organization_id), transaction.atomic():
            for pk, error in results.items():
                record = models.VerifiedDomain.objects.select_for_update().filter(pk=pk).first()
                if record is None or record.status != models.VerifiedDomain.Status.VERIFIED:
                    continue
                report.checked += 1
                record.last_checked_at = timezone.now()
                record.last_error_code = error
                record.failure_count = 0 if not error else record.failure_count + 1
                changed = {"last_checked_at", "last_error_code", "failure_count"}
                if error:
                    report.failures += 1
                if record.failure_count >= MAX_FAILURES:
                    record.status = models.VerifiedDomain.Status.FAILED
                    changed.add("status")
                    _drop_claim(record.domain, organization_id)
                    report.revoked += 1
                record.save(update_fields=changed)
    return report
