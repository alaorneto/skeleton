"""Admission policy and join requests of an organization (#26).

An organization opts in to requests in ``OrganizationAdmissionPolicy`` (no row: everything
off). A person with a verified email asks to join; the administrator or the people manager
approves or rejects, and the approval creates (or, as a human decision, reactivates) the
membership with the request's role. Links (#27), verified domains (#28) and SSO with approval
(#31) reuse ``JoinRequest`` with their own channel.

Lock order is the same as every administrative change: Organization, then the request, then
the membership. The requester never has a tenant: submission and withdrawal open
``system_context(reason="join:request")`` for one organization, chosen by this code after its
own checks and never by a body or header. Deciding runs in the approver's tenant context.
``OrganizationDirectoryEntry`` mirrors the two flags read before any tenant exists (listing in
``/join/`` and accepting requests); it is written only here, in the policy's transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.context import current_context, own_rows, system_context

REQUEST_TTL = timedelta(days=30)
MESSAGE_MAX = 500
JOIN_REASON = "join:request"
# What the administrator configures: requests (#26), links (#27) and verified domains (#28).
POLICY_FLAGS = (
    "discoverable",
    "join_requests_enabled",
    "join_links_enabled",
    "signup_via_link_enabled",
    "domain_join_enabled",
)
POLICY_FIELDS = (*POLICY_FLAGS, "request_default_role")
# Submissions per person and window, counted before anything about the organization is read.
RATE_LIMIT = 10
RATE_WINDOW_S = 3600


class JoinUnavailable(Exception):
    """Neutral: the organization does not exist, is inactive or does not accept requests."""


@dataclass(frozen=True)
class JoinRateLimited(Exception):
    retry_after: int


class AlreadyMember(Exception):
    """The person already has an active membership in the organization."""


def _models() -> Any:
    from . import models

    return models


# --------------------------------------------------------------------------- policy


def current_policy() -> Any:
    """The policy row of the context organization, or ``None`` while it was never written."""
    return (
        _models().OrganizationAdmissionPolicy.objects.filter(organization_id=current_context().organization_id).first()
    )


def revision_of(record: Any) -> int:
    return record.revision if record is not None else 0


def _role(value: Any, organization_id: UUID) -> Any:
    from skeleton.access.models import Role

    if value is None or value == "":
        return None
    if not isinstance(value, Role):
        try:
            value = Role.objects.filter(pk=UUID(str(value))).first()
        except ValueError:
            value = None
    if value is None or value.organization_id != organization_id or value.deleted_at is not None:
        raise ValidationError({"request_default_role": "Escolha um papel ativo desta organização."})
    if value.is_builtin and value.code == "organization_admin":
        # Full administration is designated or invited, never granted by a queue.
        raise ValidationError({"request_default_role": "O papel de administrador não é concedido por pedido."})
    return value


def configure_policy(*, actor: Any, data: dict[str, Any], expected_revision: int) -> Any:
    """Replace the policy with CAS (virtual revision 0); keeps the directory index in step."""
    from skeleton.access.capabilities import ADMISSION_CONFIGURE
    from skeleton.access.services import organization_lock

    models = _models()
    unknown = set(data) - set(POLICY_FIELDS)
    if unknown:
        raise ValidationError({name: "Campo não permitido." for name in sorted(unknown)})
    if type(expected_revision) is not int:
        raise ValidationError({"revision": "Informe a revisão."})
    values: dict[str, Any] = {}
    for name in POLICY_FLAGS:
        value = data.get(name, False)
        if type(value) is not bool:
            raise ValidationError({name: "Use true ou false."})
        values[name] = value
    with organization_lock(actor, capability=ADMISSION_CONFIGURE) as organization:
        values["request_default_role"] = _role(data.get("request_default_role"), organization.pk)
        record = (
            models.OrganizationAdmissionPolicy.objects.select_for_update().filter(organization=organization).first()
        )
        if revision_of(record) != expected_revision:
            raise ConcurrentUpdate("Admission policy changed.")
        if record is None:
            record = models.OrganizationAdmissionPolicy(organization=organization, **values)
            record.save()
        else:
            changed = {name for name, value in values.items() if getattr(record, name) != value}
            for name in changed:
                setattr(record, name, values[name])
            if changed:
                record.save(update_fields=changed)
        _sync_directory(organization, record)
        return record


def _sync_directory(organization: Any, policy: Any) -> None:
    models = _models()
    values = {
        "discoverable": policy.discoverable and policy.join_requests_enabled,
        "accepts_requests": policy.join_requests_enabled,
        "accepts_domain": policy.domain_join_enabled,
    }
    entry = models.OrganizationDirectoryEntry.objects.filter(listed_organization=organization).first()
    if entry is None:
        if any(values.values()):
            models.OrganizationDirectoryEntry(listed_organization=organization, **values).save()
        return
    changed = {name for name, value in values.items() if getattr(entry, name) != value}
    for name, value in values.items():
        setattr(entry, name, value)
    if changed:
        entry.save(update_fields=changed)


def default_role(policy: Any) -> Any:
    from skeleton.access.models import Role

    if policy is not None and policy.request_default_role_id:
        return policy.request_default_role
    return Role.objects.get(code="member", is_builtin=True)


# --------------------------------------------------------------------------- discovery


@dataclass(frozen=True)
class DirectoryEntry:
    organization_id: UUID
    name: str


def has_verified_email(user: Any) -> bool:
    from allauth.account.models import EmailAddress

    return bool(
        getattr(user, "is_authenticated", False)
        and user.is_active
        and user.deleted_at is None
        and EmailAddress.objects.filter(user=user, email__iexact=user.email, verified=True).exists()
    )


def directory(user: Any) -> tuple[DirectoryEntry, ...]:
    """Organizations that chose to be listed and accept requests, minus the person's own."""
    if not has_verified_email(user):
        return ()
    from .context import memberships_for_user

    mine = {item["organization_id"] for item in memberships_for_user(user)}
    rows = (
        _models()
        .OrganizationDirectoryEntry.objects.filter(
            discoverable=True,
            accepts_requests=True,
            listed_organization__is_active=True,
            listed_organization__deleted_at__isnull=True,
        )
        .exclude(listed_organization_id__in=mine)
        .order_by("listed_organization__name")
        .values_list("listed_organization_id", "listed_organization__name")
    )
    return tuple(DirectoryEntry(organization_id=pk, name=name) for pk, name in rows)


def accepting(organization_id: Any) -> DirectoryEntry:
    """The organization if it accepts requests; ``JoinUnavailable`` (neutral) otherwise."""
    try:
        organization_id = UUID(str(organization_id))
    except ValueError:
        raise JoinUnavailable from None
    row = (
        _models()
        .OrganizationDirectoryEntry.objects.filter(
            listed_organization_id=organization_id,
            accepts_requests=True,
            listed_organization__is_active=True,
            listed_organization__deleted_at__isnull=True,
        )
        .values_list("listed_organization_id", "listed_organization__name")
        .first()
    )
    if row is None:
        raise JoinUnavailable
    return DirectoryEntry(organization_id=row[0], name=row[1])


# --------------------------------------------------------------------------- requester


def effective_status(join_request: Any) -> str:
    """``expired`` is derived: an open request past ``expires_at`` can no longer be decided."""
    models = _models()
    if join_request.status in models.JoinRequest.OPEN and join_request.expires_at <= timezone.now():
        return models.JoinRequest.Status.EXPIRED
    return join_request.status


def _rate_limit(user: Any) -> None:
    from skeleton.common.ratelimit import hit

    result = hit(f"join-request:{user.pk}", limit=RATE_LIMIT, window_s=RATE_WINDOW_S)
    if not result.allowed:
        raise JoinRateLimited(result.retry_after)


def _clean_message(message: Any) -> str:
    if message is None:
        return ""
    if not isinstance(message, str):
        raise ValidationError({"message": "Use um texto."})
    message = message.strip()
    if len(message) > MESSAGE_MAX:
        raise ValidationError({"message": f"Use até {MESSAGE_MAX} caracteres."})
    return message


def submit_join_request(*, user: Any, organization_id: Any, message: Any = "") -> Any:
    """Ask to join; idempotent while a request is open. Neutral 404 for anything not accepting."""
    from .models import Membership, Organization

    models = _models()
    if not has_verified_email(user):
        raise PermissionDenied("A verified email is required to ask to join.")
    message = _clean_message(message)
    _rate_limit(user)  # Before any read about the organization: 429 reveals nothing.
    target = accepting(organization_id)
    with system_context(reason=JOIN_REASON, organization_id=target.organization_id), transaction.atomic():
        organization = (
            Organization.objects.select_for_update().filter(pk=target.organization_id, is_active=True).first()
        )
        policy = models.OrganizationAdmissionPolicy.objects.filter(organization=organization).first()
        if organization is None or policy is None or not policy.join_requests_enabled:
            raise JoinUnavailable
        if Membership.objects.filter(user=user, is_active=True).exists():
            raise AlreadyMember
        open_request = (
            models.JoinRequest.objects.select_for_update().filter(user=user, status__in=models.JoinRequest.OPEN).first()
        )
        if open_request is not None and effective_status(open_request) != models.JoinRequest.Status.EXPIRED:
            return open_request
        if open_request is not None:
            open_request.status = models.JoinRequest.Status.EXPIRED
            open_request.save(update_fields={"status"})
        record = models.JoinRequest(
            organization=organization,
            user=user,
            channel=models.JoinRequest.Channel.DIRECTORY,
            role=default_role(policy),
            status=models.JoinRequest.Status.PENDING,
            message=message,
            expires_at=timezone.now() + REQUEST_TTL,
        )
        record.save()
        return record


def my_join_requests(user: Any) -> list[Any]:
    """The person's requests in every organization (self-discovery), newest first."""
    models = _models()
    return list(own_rows(models.JoinRequest, user).select_related("organization").order_by("-created_at")[:50])


def withdraw_join_request(*, user: Any, join_request_id: Any, expected_revision: int) -> Any:
    from .models import Organization

    models = _models()
    try:
        join_request_id = UUID(str(join_request_id))
    except ValueError:
        raise JoinUnavailable from None
    organization_id = (
        own_rows(models.JoinRequest, user).filter(pk=join_request_id).values_list("organization_id", flat=True).first()
    )
    if organization_id is None:
        raise JoinUnavailable
    with system_context(reason=JOIN_REASON, organization_id=organization_id), transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization_id)
        record = (
            models.JoinRequest.objects.select_for_update(of=("self",))
            .select_related("organization")
            .filter(pk=join_request_id, user=user)
            .first()
        )
        if record is None:
            raise JoinUnavailable
        if type(expected_revision) is not int or record.revision != expected_revision:
            raise ConcurrentUpdate("Join request changed.")
        if effective_status(record) not in models.JoinRequest.OPEN:
            raise ValidationError({"status": ValidationError("O pedido não está mais aberto.", code="not_open")})
        record.status = models.JoinRequest.Status.WITHDRAWN
        record.decided_at = timezone.now()
        record.save(update_fields={"status", "decided_at"})
        return record


# --------------------------------------------------------------------------- deciders


def pending_requests() -> Any:
    """Open, unexpired requests of the context organization (RLS keeps other tenants out)."""
    models = _models()
    return (
        models.JoinRequest.objects.filter(status=models.JoinRequest.Status.PENDING, expires_at__gt=timezone.now())
        .select_related("user", "role")
        .order_by("created_at")
    )


def previous_members(user_ids: list[Any]) -> set[Any]:
    """Who of ``user_ids`` had a membership here before: approving them reactivates it."""
    from .models import Membership

    return set(
        Membership.all_objects.filter(organization_id=current_context().organization_id, user_id__in=user_ids)
        .exclude(is_active=True, deleted_at__isnull=True)
        .values_list("user_id", flat=True)
    )


def decide_join_request(*, actor: Any, join_request_id: Any, approve: bool, expected_revision: int, request=None):
    """Approve or reject with CAS; one of two decisions on the same revision wins."""
    from skeleton.access.capabilities import ADMISSION_DECIDE
    from skeleton.access.services import admit_member, organization_lock, require_unprivileged_target

    models = _models()
    if type(approve) is not bool:
        raise ValidationError({"decision": "Use approve ou reject."})
    with organization_lock(actor, capability=ADMISSION_DECIDE) as organization:
        record = (
            models.JoinRequest.objects.select_for_update()
            .select_related("user", "role")
            .filter(pk=join_request_id)
            .first()
        )
        if record is None:
            raise JoinUnavailable
        if type(expected_revision) is not int or record.revision != expected_revision:
            raise ConcurrentUpdate("Join request changed.")
        if effective_status(record) != models.JoinRequest.Status.PENDING:
            raise ValidationError({"status": ValidationError("O pedido não está mais pendente.", code="not_pending")})
        if approve:
            # A people manager cannot let someone in with a role that carries capabilities.
            require_unprivileged_target(actor, role=record.role)
            admit_member(organization=organization, user=record.user, role=record.role)
        record.status = models.JoinRequest.Status.APPROVED if approve else models.JoinRequest.Status.REJECTED
        record.decided_at = timezone.now()
        record.decided_by = actor.pk
        record.save(update_fields={"status", "decided_at", "decided_by"})
        _notify_decision(record, organization, request=request)
        return record


def _notify_decision(record: Any, organization: Any, *, request=None) -> None:
    from django.core.mail import EmailMultiAlternatives
    from django.urls import reverse

    from skeleton.common.mail import schedule_mail

    name = " ".join(organization.name.splitlines())
    approved = record.status == _models().JoinRequest.Status.APPROVED
    path = reverse("web:dashboard", kwargs={"organization_id": organization.pk}) if approved else reverse("web:home")
    origin = getattr(settings, "SKELETON_PUBLIC_ORIGIN", "")
    url = request.build_absolute_uri(path) if request is not None else (origin.rstrip("/") + path if origin else "")
    text = (
        f"Seu pedido para entrar em {name} foi aprovado."
        if approved
        else f"Seu pedido para entrar em {name} não foi aprovado."
    )
    body = text + (f"\n\n{url}" if url else "")
    message = EmailMultiAlternatives(
        f"Solicitação de ingresso: {name}", body, settings.DEFAULT_FROM_EMAIL, [record.user.email]
    )
    # Materialized now; sent only after the commit, so a rollback sends nothing.
    schedule_mail(message, request=request, target_sysid=record.pk)
