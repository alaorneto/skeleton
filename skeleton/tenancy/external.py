"""Invitations and admission of external people to the portal (#532).

An external invitation is issued for one record of an app (``app_label`` and an opaque
``subject_ref``) by someone with ``external.manage`` or by the app itself after authorizing its own
act; only the token digest is stored, and the token appears once, in the emailed link. On
acceptance, in ``system_context(reason="external:validated")``, the account whose **verified**
email is the invited one gets (or gets back) its ``ExternalAccess``, the invitation is marked, and
the admission handler the app registered (``register_external_admission``) links the access to its
record, in the same transaction: a failing handler undoes everything. Every refusal of a token
(unknown, expired, used, revoked, another organization, another email) answers the same.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.accounts.emails import normalize_email

ACCEPT_ATTEMPTS_PER_HOUR = 20
_handlers: dict[str, Callable[[UUID, UUID, UUID], None]] = {}


class InvalidExternalInvitation(ValidationError):
    def __init__(self) -> None:
        super().__init__("Invalid invitation.", code="invalid_invitation")


class ExternalDisabled(ValidationError):
    def __init__(self) -> None:
        super().__init__(gettext("O portal externo está desligado nesta organização."), code="external_disabled")


class TooManyAttempts(ValidationError):
    def __init__(self) -> None:
        super().__init__(gettext("Muitas tentativas. Tente de novo mais tarde."), code="too_many_attempts")


@dataclass(frozen=True)
class ExternalProof:
    sysid: UUID
    organization_id: UUID
    email: str
    app_label: str
    subject_ref: UUID
    token_digest: str


def register_external_admission(app_label: str, handler: Callable[[UUID, UUID, UUID], None]) -> None:
    """``handler(access_id, user_id, subject_ref)`` links an accepted access to the app's record, in the acceptance."""
    if not isinstance(app_label, str) or not app_label or not callable(handler):
        raise ValueError("register_external_admission(app_label, handler) requires a label and a callable.")
    existing = _handlers.get(app_label)
    if existing is not None and existing is not handler:
        raise ValueError(f"{app_label} already registered an external admission handler.")
    _handlers[app_label] = handler


def _digest(token: Any) -> str:
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise InvalidExternalInvitation()
    return hashlib.sha256(token.encode()).hexdigest()


def policy() -> Any:
    """The current organization's portal policy (a default, unsaved, when there is no row)."""
    from skeleton.tenancy.context import current_context
    from skeleton.tenancy.models import ExternalAccessPolicy

    found = ExternalAccessPolicy.objects.first()
    return found or ExternalAccessPolicy(organization_id=current_context().organization_id)


def configure_policy(*, user: Any, enabled: bool, invitation_ttl_hours: int, expected_revision: int) -> Any:
    """Turn the portal on or off and set the invitation lifetime, with CAS from the virtual revision 0."""
    from skeleton.access.capabilities import EXTERNAL_MANAGE, require_capability
    from skeleton.common.exceptions import ConcurrentUpdate
    from skeleton.tenancy.models import ExternalAccessPolicy

    require_capability(user, EXTERNAL_MANAGE)
    if type(enabled) is not bool or type(invitation_ttl_hours) is not int or not 1 <= invitation_ttl_hours <= 720:
        raise ValidationError({"invitation_ttl_hours": gettext("Use de 1 a 720 horas.")})
    with transaction.atomic():
        current = ExternalAccessPolicy.objects.select_for_update().first()
        revision = current.revision if current else 0
        if expected_revision != revision:
            raise ConcurrentUpdate("The portal policy changed.")
        current = current or policy()
        current.enabled, current.invitation_ttl_hours = enabled, invitation_ttl_hours
        current.save()
        return current


def _message(organization: Any, email: str, url: str, hours: int) -> EmailMultiAlternatives:
    from django.template.loader import render_to_string
    from django.utils import translation

    from skeleton.common.locale import preferences_for
    from skeleton.tenancy.email_brand import attach_logo, email_brand, template_context

    brand = email_brand()
    preferences = preferences_for(None)
    context = {
        **template_context(brand),
        "organization_name": brand["name"] or organization.name,
        "url": url,
        "hours": hours,
    }
    with translation.override(preferences.language), timezone.override(preferences.timezone):
        subject = render_to_string("skeleton/tenancy/external_invitation/subject.txt", context)
        text = render_to_string("skeleton/tenancy/external_invitation/body.txt", context)
        html = render_to_string("skeleton/tenancy/external_invitation/body.html", context)
    message = EmailMultiAlternatives(
        " ".join(subject.split())[:200], text.strip() + "\n", settings.DEFAULT_FROM_EMAIL, [email]
    )
    message.attach_alternative(html, "text/html")
    attach_logo(message, brand)
    return message


def issue(
    *,
    actor: Any,
    email: str,
    app_label: str,
    subject_ref: UUID,
    idempotency_key: str = "",
    request: Any = None,
    by_app: bool = False,
) -> Any:
    """Invite an external person for ``(app_label, subject_ref)``; a live invitation for it is revoked.

    ``by_app`` means the calling app is ``app_label`` and authorized its own act; otherwise the
    actor needs ``external.manage``. A repeated ``idempotency_key`` returns the same invitation
    without a new email.
    """
    from skeleton.access.capabilities import EXTERNAL_MANAGE, require_capability
    from skeleton.accounts.services import normalized_email
    from skeleton.common.mail import schedule_mail
    from skeleton.system.applications import require_application_enabled
    from skeleton.tenancy.context import current_context
    from skeleton.tenancy.models import ExternalInvitation, Organization

    from .site import shareable_url

    if not by_app:
        require_capability(actor, EXTERNAL_MANAGE)
    require_application_enabled(app_label)
    current = policy()
    if not current.enabled:
        raise ExternalDisabled()
    if idempotency_key:
        existing = ExternalInvitation.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            return existing
    organization_id = current_context().organization_id
    organization = Organization.objects.get(pk=organization_id, is_active=True)
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        for live in ExternalInvitation.objects.select_for_update().filter(
            app_label=app_label, subject_ref=subject_ref, accepted_at__isnull=True
        ):
            live.delete()  # The previous invitation of the same record stops validating.
        invitation = ExternalInvitation(
            organization=organization,
            email=normalized_email(email),
            app_label=app_label,
            subject_ref=subject_ref,
            token_digest=_digest(token),
            expires_at=timezone.now() + timedelta(hours=current.invitation_ttl_hours),
            idempotency_key=idempotency_key,
        )
        invitation.save()
        path = reverse("external-invitation", kwargs={"organization_id": organization_id, "token": token})
        url = shareable_url(request, organization_id, path)
        if url is None:
            raise ValidationError("Invitation email requires a request or SKELETON_PUBLIC_URL.")
        message = _message(organization, invitation.email, url, current.invitation_ttl_hours)
        schedule_mail(message, request=request, target_sysid=invitation.pk)
        return invitation


def validate(organization_id: Any, token: Any) -> ExternalProof:
    """The proof of a live invitation; anything else is ``InvalidExternalInvitation``."""
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import ExternalInvitation, Organization

    digest = _digest(token)
    with system_context(reason="external:validated", organization_id=organization_id):
        organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
        invitation = ExternalInvitation.objects.filter(
            token_digest=digest, accepted_at__isnull=True, expires_at__gt=timezone.now()
        ).first()
        if organization is None or invitation is None:
            raise InvalidExternalInvitation()
        return ExternalProof(
            invitation.pk, organization.pk, invitation.email, invitation.app_label, invitation.subject_ref, digest
        )


def _throttle(user: Any, ip: str | None) -> None:
    from skeleton.common.ratelimit import hit

    keys = [f"external:accept:user:{user.pk}"]
    if ip:
        keys.append(f"external:accept:ip:{ip}")
    for key in keys:
        if not hit(key, limit=ACCEPT_ATTEMPTS_PER_HOUR, window_s=3600).allowed:
            raise TooManyAttempts()


def accept(*, user: Any, organization_id: Any, token: Any, ip: str | None = None) -> Any:
    """Accept an external invitation with the account whose verified email is the invited one."""
    from allauth.account.models import EmailAddress

    from skeleton.common.entitlements import check_limit
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import ExternalAccess, ExternalInvitation, Organization

    _throttle(user, ip)
    proof = validate(organization_id, token)
    if not user.is_active or user.deleted_at or normalize_email(user.email) != proof.email:
        raise InvalidExternalInvitation()
    if not EmailAddress.objects.filter(user=user, email__iexact=proof.email, verified=True).exists():
        raise InvalidExternalInvitation()
    with system_context(reason="external:validated", organization_id=organization_id):
        with transaction.atomic():
            organization = Organization.objects.select_for_update(no_key=True).get(pk=organization_id, is_active=True)
            invitation = (
                ExternalInvitation.objects.select_for_update()
                .filter(pk=proof.sysid, token_digest=proof.token_digest, accepted_at__isnull=True)
                .filter(expires_at__gt=timezone.now())
                .first()
            )
            if invitation is None or not policy().enabled:
                raise InvalidExternalInvitation()
            access = ExternalAccess.objects.select_for_update().filter(user=user).first()
            if access is None or not access.is_active:
                check_limit(organization.pk, "external_people", 1)  # The plan's external people.
            if access is None:
                access = ExternalAccess(organization=organization, user=user, app_label=invitation.app_label)
                access.save()
            elif not access.is_active:
                access.is_active = True
                access.save(update_fields={"is_active"})
            invitation.accepted_at, invitation.accepted_by, invitation.access = timezone.now(), user.pk, access
            invitation.save(update_fields={"accepted_at", "accepted_by", "access"})
            handler = _handlers.get(invitation.app_label)
            if handler is not None:
                handler(access.pk, user.pk, invitation.subject_ref)
            return access


def _require_manager(user: Any) -> None:
    from skeleton.access.capabilities import EXTERNAL_MANAGE, require_capability

    require_capability(user, EXTERNAL_MANAGE)


def _locked(model: Any, sysid: Any, expected_revision: Any) -> Any:
    from skeleton.common.exceptions import ConcurrentUpdate

    found = model.objects.select_for_update().filter(pk=sysid).first()
    if found is None:
        raise PermissionDenied("Record unavailable.")
    if type(expected_revision) is not int or found.revision != expected_revision:
        raise ConcurrentUpdate("The record changed.")
    return found


@transaction.atomic
def revoke_invitation(*, user: Any, invitation_id: Any, expected_revision: Any, by_app: bool = False) -> None:
    from skeleton.tenancy.models import ExternalInvitation

    if not by_app:
        _require_manager(user)
    invitation = _locked(ExternalInvitation, invitation_id, expected_revision)
    if invitation.accepted_at is not None:
        raise InvalidExternalInvitation()
    invitation.delete()


@transaction.atomic
def resend_invitation(
    *, user: Any, invitation_id: Any, expected_revision: Any, request: Any = None, by_app: bool = False
) -> Any:
    """Revoke the invitation and send a new one (new token) to the same address for the same record."""
    from skeleton.tenancy.models import ExternalInvitation

    if not by_app:
        _require_manager(user)
    invitation = _locked(ExternalInvitation, invitation_id, expected_revision)
    if invitation.accepted_at is not None:
        raise InvalidExternalInvitation()
    return issue(
        actor=user,
        email=invitation.email,
        app_label=invitation.app_label,
        subject_ref=invitation.subject_ref,
        request=request,
        by_app=by_app,
    )


@transaction.atomic
def revoke_access(*, user: Any, access_id: Any, expected_revision: Any, by_app: bool = False) -> Any:
    """Turn an external access off; the portal closes on the next request and nothing is deleted."""
    from skeleton.tenancy.models import ExternalAccess

    if not by_app:
        _require_manager(user)
    access = _locked(ExternalAccess, access_id, expected_revision)
    if access.is_active:
        access.is_active = False
        access.save(update_fields={"is_active"})
    return access
