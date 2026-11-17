"""External people for tests (#531): an organization with the portal on and people of each kind.

``external_scenario(slug)`` creates organizations A and B (B's portal also on), the owner of A
(administrator), an ``external`` person with portal access to A and no membership, a ``dual``
person who is administrator of A **and** has portal access to it, and an ``outsider``.
``acting_external(user, organization)`` opens the portal context for the person.
Tests only; data goes through the provisioning services and ``system_context``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

FIXTURE_PASSWORD = "Fixture-only-password-2026!"  # noqa: S105 - disposable password of a local test fixture


@dataclass(frozen=True)
class ExternalScenario:
    a: Any
    b: Any
    admin_a: Any
    external: Any  # Portal access to A, no membership anywhere.
    dual: Any  # Administrator of A with portal access to A too.
    outsider: Any
    password: str = FIXTURE_PASSWORD


def grant_external_access(organization: Any, user: Any, *, app_label: str = "sk_tenancy") -> Any:
    """An active ``ExternalAccess`` of ``user`` in ``organization`` (without the invitation flow)."""
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import ExternalAccess

    with system_context(reason="test:external-access", organization_id=organization.pk):
        access = ExternalAccess(organization_id=organization.pk, user=user, app_label=app_label)
        access.save()
        return access


def enable_portal(organization: Any, *, enabled: bool = True) -> Any:
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import ExternalAccessPolicy

    with system_context(reason="test:external-policy", organization_id=organization.pk):
        policy = ExternalAccessPolicy.objects.first() or ExternalAccessPolicy(organization_id=organization.pk)
        policy.enabled = enabled
        policy.save()
        return policy


def external_scenario(slug: str = "external") -> ExternalScenario:
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.services import create_organization

    with system_context(reason="test:external-identities"):
        people = {
            name: create_identity(email=f"{name}@{slug}.example.test", password=FIXTURE_PASSWORD)
            for name in ("admin-a", "admin-b", "external", "dual", "outsider")
        }
    a = create_organization(owner=people["admin-a"], slug=f"{slug}-a", name=f"{slug} A")
    b = create_organization(owner=people["admin-b"], slug=f"{slug}-b", name=f"{slug} B")
    _admit(a, people["dual"], admin=people["admin-a"])
    for organization in (a, b):
        enable_portal(organization)
    grant_external_access(a, people["external"])
    grant_external_access(a, people["dual"])
    return ExternalScenario(a, b, people["admin-a"], people["external"], people["dual"], people["outsider"])


def _admit(organization: Any, user: Any, *, admin: Any) -> None:
    """``user`` becomes an active member of ``organization`` with the builtin administrator role."""
    from skeleton.access.models import MemberRole, Role
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:external-member", organization_id=organization.pk):
        membership = Membership(organization_id=organization.pk, user=user)
        membership.save()
        role = Role.objects.get(code="organization_admin", is_builtin=True)
        MemberRole(organization_id=organization.pk, membership=membership, role=role).save()


@contextmanager
def acting_external(user: Any, organization: Any) -> Iterator[Any]:
    """The portal context of ``user`` in ``organization``; yields the user (the ``actor``)."""
    from skeleton.tenancy.context import external_context

    with external_context(organization_id=organization.pk, actor=user, source="http:portal"):
        yield user
