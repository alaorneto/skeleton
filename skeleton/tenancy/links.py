"""Shareable, revocable multi-use join links (#27).

The token (32 random bytes) appears once, when the link is created; only its SHA-256 is
stored, and the session keeps a reference with the digest, never the token. Validation and
redemption run in ``system_context(reason="join:link")`` for the link's organization, with
the usual lock order: Organization, then the link, then the membership. Expired, revoked,
exhausted and unknown links are indistinguishable (``InvalidJoinLink``).

Redemption never reactivates a membership: a person who was a member before always gets a
join request, as does every redemption of an ``approval`` link. An active member consumes
no use, and an open request makes redemption idempotent.

Signing up through a link (when the policy allows it) creates the account and an
``awaiting_verification`` request that holds one use; the email confirmation finishes it,
even on another device, by reading the person's own requests (self-discovery).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import system_context

LINK_REASON = "join:link"
SESSION_KEY = "skeleton_join_link"


class InvalidJoinLink(Exception):
    """Neutral: unknown, expired, revoked or exhausted link, or links turned off."""


@dataclass(frozen=True)
class JoinLinkProof:
    sysid: UUID
    organization_id: UUID
    organization_name: str
    token_digest: str
    mode: str
    signup_allowed: bool


@dataclass(frozen=True)
class Redemption:
    outcome: str  # "member" (already one), "admitted" or "requested"
    record: Any = None


def _digest(token: Any) -> str:
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise InvalidJoinLink
    return hashlib.sha256(token.encode()).hexdigest()


def _models() -> Any:
    from . import models

    return models


def _policy(organization: Any) -> Any:
    return _models().OrganizationAdmissionPolicy.objects.filter(organization=organization).first()


# --------------------------------------------------------------------------- management


def create_join_link(*, actor: Any, label: Any, role: Any, mode: Any, days: Any, max_uses: Any) -> tuple[Any, str]:
    """Create a link; returns ``(link, token)``. The token is never stored nor shown again."""
    from skeleton.access.capabilities import INVITATIONS_MANAGE
    from skeleton.access.services import organization_lock, require_unprivileged_target

    models = _models()
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100:
        raise ValidationError({"label": "Use um nome de 1 a 100 caracteres."})
    if mode not in models.JoinLink.Mode.values:
        raise ValidationError({"mode": "Use direct ou approval."})
    if type(days) is not int or not 1 <= days <= models.JoinLink.MAX_DAYS:
        raise ValidationError({"days": f"Use de 1 a {models.JoinLink.MAX_DAYS} dias."})
    if type(max_uses) is not int or not 1 <= max_uses <= models.JoinLink.MAX_USES:
        raise ValidationError({"max_uses": f"Use de 1 a {models.JoinLink.MAX_USES} usos."})
    with organization_lock(actor, capability=INVITATIONS_MANAGE) as organization:
        policy = _policy(organization)
        if policy is None or not policy.join_links_enabled:
            raise ValidationError({"mode": ValidationError("Links de ingresso estão desligados.", code="links_off")})
        if role is None or role.organization_id != organization.pk or role.deleted_at is not None:
            raise ValidationError({"role": "Escolha um papel ativo desta organização."})
        if role.is_builtin and role.code == "organization_admin":
            raise ValidationError({"role": "O papel de administrador não é concedido por link."})
        require_unprivileged_target(actor, role=role)
        token = secrets.token_urlsafe(32)
        link = models.JoinLink(
            organization=organization,
            label=label.strip(),
            token_digest=_digest(token),
            role=role,
            mode=mode,
            expires_at=timezone.now() + timedelta(days=days),
            max_uses=max_uses,
        )
        link.save()
        return link, token


def revoke_join_link(*, actor: Any, link_id: Any, expected_revision: Any) -> Any:
    from skeleton.access.capabilities import INVITATIONS_MANAGE
    from skeleton.access.services import organization_lock

    models = _models()
    with organization_lock(actor, capability=INVITATIONS_MANAGE):
        link = models.JoinLink.objects.select_for_update().filter(pk=link_id).first()
        if link is None:
            raise InvalidJoinLink
        if type(expected_revision) is not int or link.revision != expected_revision:
            raise ConcurrentUpdate("Join link changed.")
        link.delete()
        return link


def live_links() -> Any:
    """Live links of the context organization (revoked ones are soft-deleted), newest first."""
    return _models().JoinLink.objects.select_related("role").order_by("-created_at")


# --------------------------------------------------------------------------- redemption


def validate_join_link(organization_id: Any, token: Any = None, *, digest: Any = None, link_id: Any = None):
    from .models import Organization

    models = _models()
    digest = _digest(token) if digest is None else digest
    try:
        organization_id = UUID(str(organization_id))
    except ValueError:
        raise InvalidJoinLink from None
    with system_context(reason=LINK_REASON, organization_id=organization_id):
        organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
        policy = _policy(organization) if organization else None
        links = models.JoinLink.objects.filter(token_digest=digest, expires_at__gt=timezone.now())
        if link_id is not None:
            links = links.filter(pk=link_id)
        link = links.first()
        if (
            organization is None
            or policy is None
            or not policy.join_links_enabled
            or link is None
            or link.use_count >= link.max_uses
        ):
            raise InvalidJoinLink
        return JoinLinkProof(
            sysid=link.pk,
            organization_id=organization.pk,
            organization_name=organization.name,
            token_digest=digest,
            mode=link.mode,
            signup_allowed=policy.signup_via_link_enabled,
        )


def remember(request: Any, proof: JoinLinkProof) -> None:
    request.session[SESSION_KEY] = {
        "organization": str(proof.organization_id),
        "link": str(proof.sysid),
        "digest": proof.token_digest,
    }


def forget(request: Any) -> None:
    request.session.pop(SESSION_KEY, None)


def pending_join_link(request: Any) -> JoinLinkProof | None:
    """The link this session opened, revalidated; ``None`` (and forgotten) when no longer valid."""
    reference = getattr(request, "session", {}).get(SESSION_KEY)
    if not isinstance(reference, dict) or set(reference) != {"organization", "link", "digest"}:
        return None
    try:
        return validate_join_link(reference["organization"], digest=reference["digest"], link_id=reference["link"])
    except (InvalidJoinLink, ValueError, ValidationError):
        forget(request)
        return None


def _locked_link(proof: JoinLinkProof) -> tuple[Any, Any, Any]:
    from .models import Organization

    models = _models()
    organization = (
        Organization.objects.select_for_update(no_key=True).filter(pk=proof.organization_id, is_active=True).first()
    )
    policy = _policy(organization) if organization else None
    link = (
        models.JoinLink.objects.select_for_update()
        .select_related("role")
        .filter(pk=proof.sysid, token_digest=proof.token_digest, expires_at__gt=timezone.now())
        .first()
    )
    if organization is None or policy is None or not policy.join_links_enabled or link is None:
        raise InvalidJoinLink
    return organization, policy, link


def _consume(link: Any) -> None:
    if link.use_count >= link.max_uses:
        raise InvalidJoinLink
    link.use_count += 1
    link.save(update_fields={"use_count"})


def _request(organization: Any, user: Any, link: Any, *, status: str) -> Any:
    from .admission import REQUEST_TTL

    record = _models().JoinRequest(
        organization=organization,
        user=user,
        channel=_models().JoinRequest.Channel.LINK,
        join_link=link,
        role=link.role,
        status=status,
        expires_at=timezone.now() + REQUEST_TTL,
    )
    record.save()
    return record


def _open_request(user: Any) -> Any:
    """The person's open request here; an expired one is closed so a new one can replace it."""
    from .admission import effective_status

    models = _models()
    record = (
        models.JoinRequest.objects.select_for_update().filter(user=user, status__in=models.JoinRequest.OPEN).first()
    )
    if record is not None and effective_status(record) == models.JoinRequest.Status.EXPIRED:
        record.status = models.JoinRequest.Status.EXPIRED
        record.save(update_fields={"status"})
        return None
    return record


def redeem_join_link(*, user: Any, proof: JoinLinkProof) -> Redemption:
    """Use the link as the authenticated ``user``; one use per admission or new request."""
    from skeleton.access.services import admit_member

    from .admission import has_verified_email
    from .models import Membership

    if not has_verified_email(user):
        raise PermissionDenied("A verified email is required to use a join link.")
    with system_context(reason=LINK_REASON, organization_id=proof.organization_id), transaction.atomic():
        organization, _, link = _locked_link(proof)
        if Membership.objects.filter(user=user, is_active=True).exists():
            return Redemption("member")
        existing = _open_request(user)
        if existing is not None:
            return Redemption("requested", existing)
        _consume(link)
        returning = Membership.all_objects.filter(user=user).exists()
        if link.mode == link.Mode.DIRECT and not returning:
            return Redemption("admitted", admit_member(organization=organization, user=user, role=link.role))
        # Automatic admission never undoes an offboarding: a former member asks again.
        return Redemption("requested", _request(organization, user, link, status="pending"))


# --------------------------------------------------------------------------- signup through a link


def start_link_signup(*, user: Any, proof: JoinLinkProof) -> Any:
    """Right after the signup: hold one use in an ``awaiting_verification`` request."""
    if not proof.signup_allowed:
        raise InvalidJoinLink
    with system_context(reason=LINK_REASON, organization_id=proof.organization_id), transaction.atomic():
        organization, policy, link = _locked_link(proof)
        if not policy.signup_via_link_enabled:
            raise InvalidJoinLink
        _consume(link)
        return _request(organization, user, link, status="awaiting_verification")


def finish_link_signup(user: Any) -> Any:
    """On email confirmation: admit (direct link) or turn the request into a pending one."""
    from skeleton.access.services import admit_member

    from .admission import has_verified_email
    from .context import own_rows, verified_owner
    from .models import Membership

    models = _models()
    if not has_verified_email(user):
        return None
    with verified_owner(user):
        found = (
            own_rows(models.JoinRequest, user)
            .filter(status=models.JoinRequest.Status.AWAITING_VERIFICATION, channel=models.JoinRequest.Channel.LINK)
            .order_by("-created_at")
            .values_list("pk", "organization_id")
            .first()
        )
        if found is None:
            return None
        request_id, organization_id = found
        with system_context(reason=LINK_REASON, organization_id=organization_id), transaction.atomic():
            from .models import Organization

            organization = Organization.objects.select_for_update(no_key=True).filter(pk=organization_id).first()
            record = (
                models.JoinRequest.objects.select_for_update(of=("self",))
                .select_related("join_link", "role")
                .filter(pk=request_id, status=models.JoinRequest.Status.AWAITING_VERIFICATION)
                .first()
            )
            if record is None or organization is None:
                return None
            if record.expires_at <= timezone.now():
                record.status = models.JoinRequest.Status.EXPIRED
                record.save(update_fields={"status"})
                return record
            policy = _policy(organization)
            link = record.join_link
            direct = (
                organization.is_active
                and policy is not None
                and policy.join_links_enabled
                and link is not None
                and link.deleted_at is None
                and link.mode == link.Mode.DIRECT
                and not Membership.all_objects.filter(user=user).exists()
            )
            if direct:
                admit_member(organization=organization, user=user, role=record.role)
                record.status = models.JoinRequest.Status.APPROVED
                record.decided_at = timezone.now()
                record.save(update_fields={"status", "decided_at"})
            else:
                record.status = models.JoinRequest.Status.PENDING
                record.save(update_fields={"status"})
            return record
