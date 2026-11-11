"""Organizational capabilities carried only by builtin roles declared in code.

A custom role never carries a capability, so editing roles cannot escalate an
administrative privilege. ``organization_admin`` remains the only full
administrator: it keeps the visibility exception in ``authorized_queryset``,
the last-administrator protection and the SQL deactivation guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import PermissionDenied

if TYPE_CHECKING:
    from skeleton.accounts.models import User

MEMBERS_VIEW = "members.view"
MEMBERS_MANAGE = "members.manage"
INVITATIONS_MANAGE = "invitations.manage"
ADMISSION_DECIDE = "admission.decide"
ADMISSION_CONFIGURE = "admission.configure"
APPS_MANAGE = "apps.manage"
ACCESS_MANAGE = "access.manage"
IDENTITY_CONFIGURE = "identity.configure"
# Service accounts and, later, their tokens and webhooks (#44); only the administrator holds it.
INTEGRATIONS_MANAGE = "integrations.manage"

CAPABILITIES: frozenset[str] = frozenset(
    {
        MEMBERS_VIEW,
        MEMBERS_MANAGE,
        INVITATIONS_MANAGE,
        ADMISSION_DECIDE,
        ADMISSION_CONFIGURE,
        APPS_MANAGE,
        ACCESS_MANAGE,
        IDENTITY_CONFIGURE,
        INTEGRATIONS_MANAGE,
    }
)


@dataclass(frozen=True, slots=True)
class BuiltinRole:
    code: str
    name: str
    capabilities: frozenset[str]


BUILTIN_ROLES: dict[str, BuiltinRole] = {
    role.code: role
    for role in (
        BuiltinRole("organization_admin", "Administrador", CAPABILITIES),
        BuiltinRole(
            "people_manager",
            "Gestor de pessoas",
            frozenset({MEMBERS_VIEW, MEMBERS_MANAGE, INVITATIONS_MANAGE, ADMISSION_DECIDE}),
        ),
        BuiltinRole("app_manager", "Gestor de apps", frozenset({APPS_MANAGE})),
        BuiltinRole("member", "Membro", frozenset()),
    )
}
BUILTIN_CODES: frozenset[str] = frozenset(BUILTIN_ROLES)

# Administrative resources of the organization and the capability that manages them.
RESOURCE_CAPABILITIES: dict[str, str] = {
    "memberships": MEMBERS_MANAGE,
    "invitations": INVITATIONS_MANAGE,
    "roles": ACCESS_MANAGE,
    "groups": ACCESS_MANAGE,
    "member-roles": ACCESS_MANAGE,
    "group-members": ACCESS_MANAGE,
    "group-roles": ACCESS_MANAGE,
    "table-permissions": ACCESS_MANAGE,
    "record-grants": ACCESS_MANAGE,
}
# Reading a resource may need less than changing it (people managers list members).
RESOURCE_VIEW_CAPABILITIES: dict[str, str] = {**RESOURCE_CAPABILITIES, "memberships": MEMBERS_VIEW}


def role_capabilities(code: str, *, is_builtin: bool) -> frozenset[str]:
    """Capabilities of a role row: only builtin rows declared in code carry any."""
    if not is_builtin:
        return frozenset()
    role = BUILTIN_ROLES.get(code)
    return role.capabilities if role else frozenset()


def capabilities_for(user: User) -> frozenset[str]:
    """Effective capabilities in the current tenant, recomputed on every call (no cache).

    A request authenticated by an API token has none (#46): tokens never administer.
    """
    from .policies import credential_scopes, effective_roles

    if credential_scopes() is not None:
        return frozenset()

    try:
        rows = effective_roles(user).filter(is_builtin=True).values_list("code", flat=True)
        codes = set(rows)
    except PermissionDenied:
        return frozenset()
    result: set[str] = set()
    for code in codes:
        result |= role_capabilities(code, is_builtin=True)
    return frozenset(result)


def has_capability(user: User, capability: str) -> bool:
    if capability not in CAPABILITIES:
        raise ValueError(f"Unknown capability: {capability}")
    return capability in capabilities_for(user)


def has_any_capability(user: User) -> bool:
    return bool(capabilities_for(user))


def require_capability(user: User, capability: str) -> None:
    if not has_capability(user, capability):
        raise PermissionDenied("Organization capability required.")


def privileged_role(role) -> bool:
    """A role whose assignment would hand out an administrative capability."""
    return bool(role_capabilities(role.code, is_builtin=role.is_builtin))


def membership_holds_capability(membership) -> bool:
    """Whether a membership currently holds a capability role, directly or by a group."""
    from .models import GroupMember, GroupRole, MemberRole

    privileged = [code for code, role in BUILTIN_ROLES.items() if role.capabilities]
    if MemberRole.objects.filter(
        membership=membership, role__is_builtin=True, role__code__in=privileged, role__deleted_at__isnull=True
    ).exists():
        return True
    groups = GroupMember.objects.filter(membership=membership, group__deleted_at__isnull=True).values_list(
        "group_id", flat=True
    )
    return GroupRole.objects.filter(
        group_id__in=groups, role__is_builtin=True, role__code__in=privileged, role__deleted_at__isnull=True
    ).exists()


def require_resource_capability(user: User, capability: str) -> None:
    """Gate an administrative surface by capability.

    A delegated manager (someone holding another capability) does not see
    surfaces outside the delegation: 404, as if the resource did not exist. An
    actor without any capability keeps the historical 403 of admin-only pages.
    """
    from django.http import Http404

    held = capabilities_for(user)
    if capability in held:
        return
    if held:
        raise Http404
    raise PermissionDenied("Organization capability required.")


def resource_capability(resource: str, *, method: str) -> str:
    from django.http import Http404

    table = RESOURCE_VIEW_CAPABILITIES if method in {"GET", "HEAD", "OPTIONS"} else RESOURCE_CAPABILITIES
    try:
        return table[resource]
    except KeyError:
        raise Http404 from None


def require_admin_surface(user: User) -> None:
    """Gate a surface reserved to the full administrator with the same 404/403 rule."""
    from .policies import is_organization_admin

    if is_organization_admin(user):
        return
    if capabilities_for(user):
        from django.http import Http404

        raise Http404
    raise PermissionDenied("Organization administrator required.")
