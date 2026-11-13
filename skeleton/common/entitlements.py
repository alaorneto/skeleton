"""Entitlement hooks: what an organization paid for and how far its access goes (#395).

The core decides availability, tenant access, background work and storage quota in its domain
packages, which must not import a subsystem (layering rule R2). So the decision points ask this
registry, and the billing subsystem (``skeleton/billing``) registers its policy in ``ready()``.
Without a registered policy everything is allowed: an installation that does not sell the
platform behaves exactly as before.

Pure Python, no ``skeleton.*`` import (rule R1). A policy answers for one organization and is
expected to memoize per request itself: the hooks call it on hot paths.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID


class AccessLevel(StrEnum):
    """How far an organization's members may go, from the billing standing of the organization."""

    ACTIVE = "active"  # Normal.
    PAST_DUE = "past_due"  # Normal access, with a warning.
    RESTRICTED = "restricted"  # Read only for people: unsafe requests and service commands refused.
    SUSPENDED = "suspended"  # Only the exempt routes, for who may settle the account; app jobs cancelled.


class EntitlementPolicy(Protocol):
    def entitled_apps(self, organization_id: UUID, app_labels: tuple[str, ...]) -> dict[str, bool]:
        """Whether the organization may use each app (only apps it has to pay for are ``False``)."""

    def organization_access(self, organization_id: UUID) -> AccessLevel: ...

    def storage_quota(self, organization_id: UUID) -> int | None | object:
        """Bytes of the organization's storage allowance, ``None`` for unlimited or ``NO_OPINION``."""

    def may_use_exempt_routes(self, organization_id: UUID, user: Any) -> bool:
        """Whether ``user`` may reach the exempt routes (settle the account) while access is limited."""


# A storage_quota answer that leaves the decision to the platform setting.
NO_OPINION = object()

_policy: EntitlementPolicy | None = None
# Path segments under /o/<id>/ and /api/v1/o/<id>/ that stay reachable while access is limited.
_exempt_segments: set[str] = set()


def register_policy(policy: EntitlementPolicy) -> None:
    """Register the single entitlement policy (in ``AppConfig.ready``); a second, different one is refused."""
    global _policy
    if _policy is not None and _policy is not policy:
        raise ValueError("An entitlement policy is already registered.")
    _policy = policy


def registered_policy() -> EntitlementPolicy | None:
    return _policy


@contextmanager
def override_policy(policy: EntitlementPolicy | None) -> Iterator[None]:
    """Use ``policy`` (or none) for the duration of the block; tests only."""
    global _policy
    previous = _policy
    _policy = policy
    try:
        yield
    finally:
        _policy = previous


def register_exempt_segment(segment: str) -> None:
    """Keep ``/o/<id>/<segment>/`` (and its API twin) reachable while access is limited."""
    if not segment or "/" in segment:
        raise ValueError("An exempt segment is one path segment, such as 'billing'.")
    _exempt_segments.add(segment)


def exempt_segments() -> frozenset[str]:
    return frozenset(_exempt_segments)


def entitled_apps(organization_id: UUID, app_labels: Iterable[str]) -> dict[str, bool]:
    labels = tuple(dict.fromkeys(app_labels))
    if _policy is None:
        return dict.fromkeys(labels, True)
    answer = _policy.entitled_apps(organization_id, labels)
    # A label the policy does not mention stays entitled: only paid apps can be closed.
    return {label: bool(answer.get(label, True)) for label in labels}


def organization_access(organization_id: UUID) -> AccessLevel:
    if _policy is None:
        return AccessLevel.ACTIVE
    return AccessLevel(_policy.organization_access(organization_id))


def storage_quota(organization_id: UUID) -> int | None | object:
    if _policy is None:
        return NO_OPINION
    return _policy.storage_quota(organization_id)


def may_use_exempt_routes(organization_id: UUID, user: Any) -> bool:
    if _policy is None:
        return True
    return bool(_policy.may_use_exempt_routes(organization_id, user))


def writes_allowed(level: AccessLevel) -> bool:
    return level in (AccessLevel.ACTIVE, AccessLevel.PAST_DUE)


def limited_code(level: AccessLevel) -> str:
    """The stable refusal code of a limited access level."""
    return "organization_suspended" if level is AccessLevel.SUSPENDED else "organization_restricted"


def require_writable() -> None:
    """Refuse a write by a person in an organization whose access is limited (#395).

    Infrastructure (``system_context``: jobs, commands) keeps writing: only what people start
    is read only. Raises ``OrganizationAccessLimited`` (a ``PermissionDenied``).
    """
    from .context import current_context
    from .exceptions import OrganizationAccessLimited

    context = current_context()
    if context.organization_id is None or context.system:
        return
    level = organization_access(context.organization_id)
    if not writes_allowed(level):
        raise OrganizationAccessLimited(limited_code(level))
