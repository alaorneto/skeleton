"""The organization's own notices about its people (#786): someone joined, someone left.

They are sent where the core publishes ``membership.created`` and ``membership.deactivated``
(``skeleton.events.core_events.membership_changed``), in the transaction of the change, like the
member limit notice (#698): a rollback leaves no notice. The payload carries only the person's
UUID; the fan-out shows the display name to whoever may manage members (``authorize``). Service
accounts never produce a notice, nor does the first membership of an organization (its creator
has nobody to tell), nor a type turned off by the platform or the organization.
"""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext_lazy

MEMBER_JOINED = "organization.member_joined"
MEMBER_LEFT = "organization.member_left"


def _manages_members(user: Any, notification: Any) -> bool:
    from skeleton.access.capabilities import MEMBERS_MANAGE, has_capability

    return str(user.pk) != str(notification.payload.get("member")) and has_capability(user, MEMBERS_MANAGE)


def _members_url(user: Any, row: Any) -> str | None:
    from django.urls import reverse

    from skeleton.access.capabilities import MEMBERS_VIEW, has_capability
    from skeleton.tenancy.context import current_context

    if not has_capability(user, MEMBERS_VIEW):
        return None
    return reverse(
        "web:access-list", kwargs={"organization_id": current_context().organization_id, "resource": "memberships"}
    )


def register_member_notices() -> None:
    from skeleton.notifications.registry import NotificationField, NotificationType, register_notification_type

    member = (NotificationField("member", gettext_lazy("Pessoa"), kind="person"),)
    common = {"payload_fields": ("member",), "owner_app": "sk_tenancy", "context": "organization"}
    register_notification_type(
        NotificationType(
            MEMBER_JOINED,
            gettext_lazy("Pessoa entrou na organização"),
            description=gettext_lazy("Quando uma pessoa passa a fazer parte da organização"),
            fields=member,
            title=gettext_lazy("{member} entrou em {organizacao}"),
            authorize=_manages_members,
            open_url=_members_url,
            **common,
        )
    )
    register_notification_type(
        NotificationType(
            MEMBER_LEFT,
            gettext_lazy("Pessoa saiu da organização"),
            description=gettext_lazy("Quando o vínculo de uma pessoa com a organização é encerrado"),
            fields=member,
            title=gettext_lazy("{member} não faz mais parte de {organizacao}"),
            authorize=_manages_members,
            open_url=_members_url,
            **common,
        )
    )


def _others_active(membership: Any) -> bool:
    """Whether someone else is an active member of the organization (the creator alone is not told)."""
    from skeleton.tenancy.models import Membership

    return Membership.objects.filter(is_active=True, user__is_active=True).exclude(pk=membership.pk).exists()


def _notify(type_code: str, recipients: Any, member: str) -> None:
    from skeleton.notifications.fanout import delivery_enabled
    from skeleton.notifications.registry import get_notification_type
    from skeleton.notifications.services import notify

    try:
        get_notification_type(type_code)
    except KeyError:
        return  # Not registered (a test with an isolated registry): nothing to send.
    if not delivery_enabled(type_code):
        return  # Turned off by the platform or the organization: not even a notice row.
    notify(type_code, recipients=recipients, payload={"member": member})


def membership_notice(membership: Any, *, active: bool) -> None:
    """Called by ``membership_changed`` in the transaction of the change, inside its organization."""
    from skeleton.notifications.services import Recipients
    from skeleton.tenancy.context import current_context

    if current_context().organization_id != membership.organization_id:
        return
    user = getattr(membership, "user", None)
    if user is None or getattr(user, "account_type", "human") != "human" or not _others_active(membership):
        return
    member = str(membership.user_id)
    _notify(MEMBER_JOINED if active else MEMBER_LEFT, Recipients(organization=True), member)
