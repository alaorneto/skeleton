"""MFA keyring and the policy that says who must enrol MFA (#740).

Rules, each one only tightening the others (none loosens another):

- ``SKELETON_REQUIRE_PLATFORM_ADMIN_MFA``: every full platform administrator; pinned on in production.
- ``SKELETON_REQUIRE_ADMIN_MFA``: whoever holds a platform role or an administrative capability in
  the organization of the request.
- ``SKELETON_REQUIRE_MFA``: every person.
- ``OrganizationSettings.mfa_policy`` of the organization of the request: ``admins`` adds whoever
  holds an administrative capability there, ``all`` every member. Never applied to the portal of
  external people, who are not members.

Service accounts never enrol MFA. A TOTP authenticator satisfies every rule.
"""

from typing import Any

from django.conf import settings

from skeleton.common.crypto import multifernet

MFA_POLICIES = frozenset({"", "admins", "all"})


def cipher():
    """The MFA keyring; same MultiFernet tokens as before the common module (no data migration)."""
    return multifernet("MFA_ENCRYPTION_KEYS", purpose="MFA")


def has_totp(user: Any) -> bool:
    from allauth.mfa.models import Authenticator

    return Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()


def _person(user: Any) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False) is True
        and not getattr(user, "is_service_account", False)
    )


def _platform_rule(user: Any) -> bool:
    """The instance-wide rules that need no organization."""
    if getattr(settings, "SKELETON_REQUIRE_MFA", False):
        return True
    from skeleton.accounts.platform import is_platform_admin

    return bool(getattr(settings, "SKELETON_REQUIRE_PLATFORM_ADMIN_MFA", False) and is_platform_admin(user))


def mfa_required(user: Any) -> bool:
    """Privileged surfaces: an administrative rule applies and the person has no TOTP.

    Callers already know the person is privileged (a platform role or an organization
    capability); the platform administrator's rule and the every-person rule apply on their own.
    """
    if not _person(user):
        return False
    if not (getattr(settings, "SKELETON_REQUIRE_ADMIN_MFA", False) or _platform_rule(user)):
        return False
    return not has_totp(user)


def organization_mfa_policy(organization_id: Any) -> str:
    """The stored policy of the context organization ("" without a row); reuses the request's read."""
    from skeleton.tenancy.organization_settings import current_settings

    record = current_settings()
    return (
        record.mfa_policy if record is not None and str(record.organization_id) == str(organization_id) else ""
    ) or ""


def mfa_demand(user: Any) -> str | None:
    """Which kind of rule demands MFA of ``user`` here, whatever they enrolled, or ``None``.

    ``platform``: a rule that holds on every page (every person, the platform administrator, a
    platform role). ``organization``: a rule that comes from the organization of the request (its
    policy, or the administrative rule over a capability held there).
    """
    if not _person(user):
        return None
    if _platform_rule(user):
        return "platform"
    admin_rule = bool(getattr(settings, "SKELETON_REQUIRE_ADMIN_MFA", False))
    if admin_rule:
        from skeleton.accounts.platform import platform_capabilities

        if platform_capabilities(user):
            return "platform"
    from skeleton.tenancy.context import current_context

    context = current_context()
    if context.organization_id is None or context.external:
        return None
    policy = organization_mfa_policy(context.organization_id)
    if policy == "all":
        return "organization"
    if (admin_rule or policy == "admins") and has_any_capability_here(user):
        return "organization"
    return None


def has_any_capability_here(user: Any) -> bool:
    from skeleton.access.capabilities import has_any_capability

    return has_any_capability(user)


def mfa_demanded(user: Any) -> bool:
    """Whether some rule demands MFA of ``user`` in the current context, whatever they enrolled."""
    return mfa_demand(user) is not None


def mfa_enrollment_pending(user: Any) -> bool:
    """A rule demands MFA in the current context and the person has not enrolled TOTP yet."""
    return mfa_demanded(user) and not has_totp(user)
