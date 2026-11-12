"""Erasure of a person's identity (#191): requested by the person, approved by the platform.

The person asks at ``/profile/privacy/`` (one open request at a time, cancellable while pending);
a platform administrator approves or rejects it at ``/platform/privacy/``. Nothing changes
before the approval. The approval runs in the platform control plane, where the last-administrator
guards (``assert_user_deactivation``, the platform seat) hold under lock, and anonymizes the
identity in the same transaction, so access ends at once:

- the identity: ``anon-<sysid>@anonymized.invalid``, empty names, inactive, unusable password,
  no local password and a new ``auth_version`` (every session ends at the next request);
- the copies the core keeps globally: allauth emails, social accounts and MFA authenticators
  deleted, API tokens revoked, push subscriptions and notification preferences removed, profile
  reset;
- then one ``accounts.anonymize_organization`` job per active organization, which deletes the
  person's notification rows there and deactivates the membership (revoking roles, groups and
  grants). A transaction selects one tenant, so each organization is its own job.

A decided request cannot be decided again (CAS), and the organization job only touches an
anonymized identity and finds nothing left the second time. Business records of the
organizations (their controllers) stay as they are and keep pointing to the same sysid; old
audit events are redacted by ``redact_audit_personal_data`` (#189). Service accounts are
refused: their organization manages them.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate

ANONYMIZE_ORGANIZATION_JOB = "accounts.anonymize_organization"
NOTE_MAX = 500


def anonymized_email(user: Any) -> str:
    from .models import ANONYMIZED_EMAIL_DOMAIN

    return f"anon-{user.pk}@{ANONYMIZED_EMAIL_DOMAIN}"


def is_anonymized(user: Any) -> bool:
    return user.email == anonymized_email(user)


def _model():
    from .models import ErasureRequest

    return ErasureRequest


def current_request(user: Any) -> Any:
    """The person's latest request, or ``None``."""
    return _model().objects.filter(user_id=user.pk).order_by("-created_at").first()


def request_erasure(user: Any) -> Any:
    """The person asks for the erasure of their own identity; an open request is returned as is."""
    from skeleton.common.logging import security_event

    ErasureRequest = _model()
    if user.is_service_account:
        raise ValidationError(gettext("Contas de serviço são geridas pela organização e não pedem eliminação."))
    with transaction.atomic():
        open_request = (
            ErasureRequest.objects.select_for_update()
            .filter(user_id=user.pk, state=ErasureRequest.State.PENDING)
            .first()
        )
        if open_request is not None:
            return open_request
        row = ErasureRequest(user_id=user.pk)
        row.save()
    security_event("privacy.erasure_requested", target_sysid=user.pk, outcome="success")
    return row


def cancel_erasure(user: Any, *, request_id: Any, expected_revision: int) -> Any:
    """The person withdraws their own pending request (another person's request is not found)."""
    ErasureRequest = _model()
    with transaction.atomic():
        row = ErasureRequest.objects.select_for_update().filter(pk=request_id, user_id=user.pk).first()
        if row is None:
            raise ValidationError(gettext("Pedido indisponível."))
        if row.revision != expected_revision or row.state != ErasureRequest.State.PENDING:
            raise ConcurrentUpdate("The erasure request changed.")
        row.state = ErasureRequest.State.CANCELLED
        row.save(update_fields={"state"})
    return row


def requests_for_platform(*, actor: Any, state: str | None = None) -> Any:
    """Requests for the platform page, open ones first."""
    from django.db.models import Case, IntegerField, Value, When

    _require_platform(actor)
    ErasureRequest = _model()
    rows = ErasureRequest.objects.select_related("user")
    if state:
        rows = rows.filter(state=state)
    order = Case(
        When(state=ErasureRequest.State.PENDING, then=Value(0)),
        default=Value(1),
        output_field=IntegerField(),
    )
    return rows.order_by(order, "-created_at")


def _require_platform(actor: Any) -> None:
    from .platform import is_platform_admin

    if not is_platform_admin(actor):
        raise PermissionDenied("Platform administration required.")


def _decide(actor: Any, request_id: Any, expected_revision: int) -> Any:
    ErasureRequest = _model()
    row = ErasureRequest.objects.select_for_update().filter(pk=request_id).first()
    if row is None:
        raise ValidationError(gettext("Pedido indisponível."))
    if row.revision != expected_revision or row.state != ErasureRequest.State.PENDING:
        raise ConcurrentUpdate("The erasure request changed.")
    if row.user_id == actor.pk:
        raise PermissionDenied(gettext("Outro administrador da plataforma precisa decidir o seu pedido."))
    return row


def _assert_can_deactivate(user: Any) -> Any:
    """``LastAdministrator`` / ``LastPlatformAdministrator`` when the person still holds a last seat."""
    from skeleton.access.services import assert_user_deactivation

    from .models import User
    from .platform import assert_platform_admin_remains, lock_platform_admins

    admins = lock_platform_admins()
    locked = User.all_objects.select_for_update().get(pk=user.pk)
    assert_platform_admin_remains(admins, locked, keeps_role=False)
    assert_user_deactivation(locked)
    return locked


def approve_erasure(*, actor: Any, request_id: Any, expected_revision: int) -> Any:
    """Approve a pending request and anonymize the identity (platform control plane only).

    ``LastAdministrator`` or ``LastPlatformAdministrator`` roll everything back: nothing changes
    until the person stops being a last administrator.
    """
    from skeleton.common.logging import security_event

    _require_platform(actor)
    ErasureRequest = _model()
    with transaction.atomic():
        row = _decide(actor, request_id, expected_revision)
        user = _assert_can_deactivate(row.user)
        _anonymize_identity(user)
        organizations = _enqueue_organization_cleanup(user, row)
        now = timezone.now()
        row.state = ErasureRequest.State.COMPLETED
        row.decided_by = actor.pk
        row.decided_at = row.completed_at = now
        row.save(update_fields={"state", "decided_by", "decided_at", "completed_at"})
    security_event("privacy.erasure_completed", target_sysid=row.user_id, outcome="success", count=organizations)
    return row


def reject_erasure(*, actor: Any, request_id: Any, expected_revision: int, note: str) -> Any:
    """Reject a pending request with a note the person sees."""
    from skeleton.common.logging import security_event

    note = (note or "").strip()
    if not note or len(note) > NOTE_MAX:
        raise ValidationError({"note": gettext("Explique a recusa em até 500 caracteres.")})
    _require_platform(actor)
    ErasureRequest = _model()
    with transaction.atomic():
        row = _decide(actor, request_id, expected_revision)
        row.state = ErasureRequest.State.REJECTED
        row.decided_by = actor.pk
        row.decided_at = timezone.now()
        row.decision_note = note
        row.save(update_fields={"state", "decided_by", "decided_at", "decision_note"})
    security_event("privacy.erasure_rejected", target_sysid=row.user_id, outcome="success")
    return row


def _remove_identity_copies(user: Any) -> None:
    from allauth.account.models import EmailAddress
    from allauth.mfa.models import Authenticator
    from allauth.socialaccount.models import SocialAccount

    from skeleton.integrations.tokens.models import ApiToken
    from skeleton.notifications.models import UserNotificationPreference, WebPushSubscription

    from .models import UserProfile

    EmailAddress.objects.filter(user_id=user.pk).delete()
    SocialAccount.objects.filter(user_id=user.pk).delete()  # Its tokens cascade.
    Authenticator.objects.filter(user_id=user.pk).delete()
    now = timezone.now()
    for token in ApiToken.objects.select_for_update().filter(user_id=user.pk, revoked_at__isnull=True):
        token.revoked_at = now
        token.save(update_fields={"revoked_at"})
    for model in (WebPushSubscription, UserNotificationPreference):
        for row in model.objects.filter(user_id=user.pk):
            row.delete()
    profile = UserProfile.objects.select_for_update().filter(user_id=user.pk).first()
    if profile is not None and (profile.display_name or profile.timezone or profile.language):
        profile.display_name = profile.timezone = profile.language = ""
        profile.save(update_fields={"display_name", "timezone", "language"})


def _anonymize_identity(user: Any) -> None:
    user.email = anonymized_email(user)
    user.first_name = user.last_name = ""
    user.is_active = False
    user.is_staff = user.is_superuser = False
    user.local_password_allowed = False
    user.set_unusable_password()
    user.auth_version += 1
    user.save(
        update_fields={
            "email",
            "first_name",
            "last_name",
            "is_active",
            "is_staff",
            "is_superuser",
            "local_password_allowed",
            "password",
            "auth_version",
        }
    )
    _remove_identity_copies(user)


def _enqueue_organization_cleanup(user: Any, row: Any) -> int:
    """One job per active organization; a job finds nothing where the person never was a member."""
    from skeleton.jobs.services import enqueue
    from skeleton.tenancy.models import Organization

    organizations = list(Organization.objects.filter(is_active=True).values_list("pk", flat=True))
    for organization_id in organizations:
        enqueue(
            ANONYMIZE_ORGANIZATION_JOB,
            organization_id=organization_id,
            payload={"user": user.pk},
            idempotency_key=f"anonymize:{row.pk}:{organization_id}",
        )
    return len(organizations)


def anonymize_organization(*, user: Any) -> int:
    """Job handler (system context of one organization): the person's rows of this tenant.

    Deletes the person's inbox rows and deliveries (a nominal exception to the notification
    retention, AGENTS.md) and deactivates the membership with its roles, groups and grants.
    Only an anonymized identity is touched, so a stray job never affects a live person.
    """
    from skeleton.access.services import _revoke_membership_links
    from skeleton.notifications.models import NotificationDelivery, NotificationRecipient
    from skeleton.tenancy.models import Membership

    from .models import User

    person = User.all_objects.filter(pk=user).first()
    if person is None or not is_anonymized(person):
        return 0
    removed, _ = NotificationDelivery.objects.filter(user_id=person.pk).delete()
    rows, _ = NotificationRecipient.objects.filter(user_id=person.pk).delete()
    membership = Membership.objects.select_for_update().filter(user_id=person.pk).first()
    if membership is not None and membership.is_active:
        from skeleton.events.core_events import membership_changed

        _revoke_membership_links(membership)
        membership.is_active = False
        membership.save(update_fields={"is_active"})
        membership_changed(membership, was_active=True)
    return removed + rows
