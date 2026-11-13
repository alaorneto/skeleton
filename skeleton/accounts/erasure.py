"""Anonymization of an identity at the person's request, approved by the platform (#191).

1. The person asks on ``/profile/privacy/`` (``request_erasure``): an ``ErasureRequest`` in
   ``pending``; nothing else changes. They may withdraw it while it is pending.
2. A platform administrator approves or rejects it on ``/platform/privacy/``
   (``decide_erasure``). The approval anonymizes the identity in its own transaction, under
   the same last-administrator guard as a deactivation (``skeleton.assert_user_deactivation``,
   which holds the organization locks until the commit, so no one can make the person the last
   administrator in between): email ``anon-<sysid>@erased.invalid``, names empty, inactive, no usable
   password, ``auth_version`` incremented (every session ends), the profile back to its
   defaults, the allauth email addresses, social accounts and MFA authenticators deleted, the
   API tokens revoked and the notification preferences and push subscriptions removed.
3. The same transaction enqueues ``accounts.forget_in_organization`` for every organization,
   which deletes the person's inbox rows and deliveries and disables the webhooks acting as the
   person there (tenant rows are reachable only inside their organization).

The organization jobs are idempotent: a job that runs again finds the rows gone. Business records
of the organizations are never touched: they keep pointing at the same, now anonymous, identity
(``PROTECT`` relations), and the memberships stay as history. The audit keeps its old events;
redacting them is ``redact_audit_personal_data`` (#189).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.common.logging import security_event

if TYPE_CHECKING:
    from skeleton.accounts.models import ErasureRequest, User

FORGET_JOB = "accounts.forget_in_organization"
ANONYMOUS_DOMAIN = "erased.invalid"


def anonymous_email(user_id: UUID) -> str:
    return f"anon-{user_id}@{ANONYMOUS_DOMAIN}"


def _refuse_service_account(user: Any) -> None:
    if getattr(user, "is_service_account", False):
        raise ValidationError(gettext("Contas de serviço não pedem eliminação; desative-as na organização."))


def open_request(user: Any) -> ErasureRequest | None:
    """The person's pending request, if any."""
    from skeleton.accounts.models import ErasureRequest

    return (
        ErasureRequest.objects.filter(user_id=user.pk, status__in=ErasureRequest.OPEN).order_by("-created_at").first()
    )


def request_erasure(user: User) -> ErasureRequest:
    """The authenticated person asks to be anonymized; nothing changes before the approval."""
    from skeleton.accounts.models import ErasureRequest
    from skeleton.tenancy.context import current_context

    context = current_context()
    if not getattr(user, "is_authenticated", False) or context.actor != user.pk or context.organization_id:
        raise PermissionDenied("Only the authenticated person asks for their own erasure.")
    _refuse_service_account(user)
    with transaction.atomic(using=context.using):
        if open_request(user) is not None:
            raise ValidationError(gettext("Já existe um pedido de eliminação em andamento."))
        record = ErasureRequest(user_id=user.pk)
        record.save()
    security_event("privacy.erasure_requested", target_sysid=str(user.pk), outcome="success")
    return record


def withdraw_erasure(user: User, *, request_id: UUID, expected_revision: int) -> ErasureRequest:
    """The person withdraws their pending request."""
    from skeleton.accounts.models import ErasureRequest
    from skeleton.tenancy.context import current_context

    context = current_context()
    if not getattr(user, "is_authenticated", False) or context.actor != user.pk:
        raise PermissionDenied("Only the authenticated person withdraws their request.")
    with transaction.atomic(using=context.using):
        record = ErasureRequest.objects.select_for_update().filter(pk=request_id, user_id=user.pk).first()
        if record is None or record.status != ErasureRequest.Status.PENDING:
            raise ValidationError(gettext("Este pedido não pode mais ser retirado."))
        if record.revision != expected_revision:
            raise ConcurrentUpdate("The erasure request changed.")
        record.status = ErasureRequest.Status.CANCELLED
        record.save(update_fields={"status"})
    return record


def decide_erasure(*, actor: User, request_id: UUID, approve: bool, expected_revision: int) -> ErasureRequest:
    """A platform administrator approves (anonymizing now) or rejects a pending request.

    The approval refuses a service account, the administrator's own request and a person who is
    the last active administrator of an organization (``LastAdministrator``); a refusal rolls
    the whole transaction back and the request stays pending.
    """
    from skeleton.accounts.models import ErasureRequest, User
    from skeleton.accounts.platform import require_platform_context
    from skeleton.tenancy.context import current_context

    require_platform_context(actor)
    context = current_context()
    with transaction.atomic(using=context.using):
        record = ErasureRequest.objects.select_for_update().filter(pk=request_id).first()
        if record is None:
            raise ErasureRequest.DoesNotExist("Erasure request unavailable.")
        if record.revision != expected_revision:
            raise ConcurrentUpdate("The erasure request changed.")
        if record.status != ErasureRequest.Status.PENDING:
            raise ValidationError(gettext("Este pedido já foi decidido."))
        if record.user_id == actor.pk:
            raise PermissionDenied(gettext("Outro administrador da plataforma decide o seu próprio pedido."))
        now = timezone.now()
        record.decided_by_id = actor.pk
        record.decided_at = now
        if approve:
            person = User.all_objects.select_for_update().get(pk=record.user_id)
            _refuse_service_account(person)
            _anonymize(person, using=context.using, now=now)
            record.status = ErasureRequest.Status.COMPLETED
            record.completed_at = now
            record.save(update_fields={"status", "decided_by_id", "decided_at", "completed_at"})
            _enqueue_forgetting(person.pk, record.pk)
        else:
            record.status = ErasureRequest.Status.REJECTED
            record.save(update_fields={"status", "decided_by_id", "decided_at"})
    security_event(
        "privacy.erasure_decided",
        target_sysid=str(record.user_id),
        outcome="approved" if approve else "rejected",
    )
    return record


def _anonymize(person: User, *, using: str, now: Any) -> None:
    from allauth.account.models import EmailAddress
    from allauth.mfa.models import Authenticator
    from allauth.socialaccount.models import SocialAccount

    from skeleton.access.services import assert_user_deactivation
    from skeleton.accounts.models import UserProfile
    from skeleton.integrations.tokens.models import ApiToken
    from skeleton.notifications.models import UserNotificationPreference, WebPushSubscription

    # The same guard as a deactivation: the last administrator of an organization stays.
    assert_user_deactivation(person, using=using)
    person.email = anonymous_email(person.pk)
    person.first_name = ""
    person.last_name = ""
    person.is_active = False
    person.local_password_allowed = False
    person.set_unusable_password()
    person.auth_version += 1
    person.save(
        update_fields=[
            "email",
            "first_name",
            "last_name",
            "is_active",
            "local_password_allowed",
            "password",
            "auth_version",
        ]
    )
    profile = UserProfile.all_objects.filter(user_id=person.pk).first()
    if profile is not None:
        profile.display_name, profile.timezone, profile.language = "", "", ""
        profile.save(update_fields=["display_name", "timezone", "language"])
    EmailAddress.objects.filter(user_id=person.pk).delete()
    # SOCIALACCOUNT_STORE_TOKENS is off and the runtime role only reads socialaccount_socialtoken:
    # delete without the cascade collector, whose (always empty) DELETE there would be refused.
    # A token row, were there one, fails the foreign key: closed, never silently kept.
    SocialAccount.objects.filter(user_id=person.pk)._raw_delete(using)
    Authenticator.objects.filter(user_id=person.pk).delete()
    for token in ApiToken.objects.filter(user_id=person.pk).order_by("sysid"):
        if token.revoked_at is None:
            token.revoked_at = now
            token.save(update_fields={"revoked_at"})
        token.delete()
    for model in (UserNotificationPreference, WebPushSubscription):
        for row in model.objects.filter(user_id=person.pk).order_by("sysid"):
            row.delete()


def _enqueue_forgetting(user_id: UUID, request_id: UUID) -> None:
    """Tenant rows are reachable only in their organization: one cleanup job each, same transaction."""
    from skeleton.jobs.services import enqueue
    from skeleton.tenancy.models import Organization

    for organization_id in Organization.all_objects.order_by("sysid").values_list("sysid", flat=True):
        enqueue(
            FORGET_JOB,
            organization_id=organization_id,
            payload={"user_id": str(user_id)},
            idempotency_key=f"erasure:{request_id}:{organization_id}",
        )


def forget_in_organization(*, user_id: str) -> None:
    """Job handler (system context of one organization): the person's inbox, deliveries, webhooks
    and the personal fields of the flow forms they sent (#422)."""
    from skeleton.flows.retention import forget_person as forget_in_flows
    from skeleton.integrations.webhooks.services import disable_endpoints_of_principal
    from skeleton.notifications.retention import forget_person

    forget_person(UUID(user_id))
    disable_endpoints_of_principal(UUID(user_id))
    forget_in_flows(UUID(user_id))


def register_erasure_jobs() -> None:
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(FORGET_JOB, forget_in_organization, {"user_id": "uuid"}, max_attempts=5, owner_app="sk_accounts")
    )
