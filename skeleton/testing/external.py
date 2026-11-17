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
    member: Any = None  # A plain member of A, without external.manage.
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
            for name in ("admin-a", "admin-b", "external", "dual", "outsider", "member")
        }
    a = create_organization(owner=people["admin-a"], slug=f"{slug}-a", name=f"{slug} A")
    b = create_organization(owner=people["admin-b"], slug=f"{slug}-b", name=f"{slug} B")
    _admit(a, people["dual"], role="organization_admin")
    _admit(a, people["member"], role="member")
    for organization in (a, b):
        enable_portal(organization)
    grant_external_access(a, people["external"])
    grant_external_access(a, people["dual"])
    return ExternalScenario(
        a, b, people["admin-a"], people["external"], people["dual"], people["outsider"], member=people["member"]
    )


def _admit(organization: Any, user: Any, *, role: str) -> None:
    """``user`` becomes an active member of ``organization`` with the builtin ``role``."""
    from skeleton.access.models import MemberRole, Role
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:external-member", organization_id=organization.pk):
        membership = Membership(organization_id=organization.pk, user=user)
        membership.save()
        builtin = Role.objects.get(code=role, is_builtin=True)
        MemberRole(organization_id=organization.pk, membership=membership, role=builtin).save()


@contextmanager
def acting_external(user: Any, organization: Any) -> Iterator[Any]:
    """The portal context of ``user`` in ``organization``; yields the user (the ``actor``)."""
    from skeleton.tenancy.context import external_context

    with external_context(organization_id=organization.pk, actor=user, source="http:portal"):
        yield user


def assert_external_contract(
    service_class: Any,
    method: str,
    *,
    scenario: ExternalScenario,
    call: Any,
    unlinked: Any = None,
    disabled: Any = None,
) -> None:
    """Prove the portal contract of one ``allow_external`` service method (#536).

    ``call(actor)`` invokes the method for ``actor`` in the current context. The kit checks that:
    the method is marked ``allow_external``; it works for the external person of A in A's portal;
    it is refused in a member context (a member without external access); an external access of
    another organization reaches nothing of A (``unlinked(result) -> bool`` says the result holds
    nothing, for a person external in B calling in B's portal); with the app off (``disabled()``,
    a context manager that turns it off in A) it is unavailable; and an actor other than the
    context's is refused. A refusal is any ``ServiceError``.
    """
    from skeleton.service_api import Forbidden, ServiceError

    from .service_api import acting_as

    spec = service_class.service_methods().get(method)
    if spec is None or not spec.allow_external:
        raise AssertionError(f"{service_class.__name__}.{method} is not marked allow_external.")
    with acting_external(scenario.external, scenario.a) as actor:
        call(actor)
        try:
            call(scenario.dual)
        except Forbidden as error:
            if error.code != "actor_mismatch":
                raise AssertionError(f"Another actor was refused with {error.code}, not actor_mismatch.") from None
        else:
            raise AssertionError("An actor other than the context's was accepted.")
    with acting_as(scenario.member, scenario.a) as member:
        try:
            call(member)
        except ServiceError:
            pass
        else:
            raise AssertionError("A member context without external access reached the method.")
    if unlinked is not None:
        grant_external_access(scenario.b, scenario.outsider)
        with acting_external(scenario.outsider, scenario.b) as stranger:
            try:
                result = call(stranger)
            except ServiceError:
                result = None
            if result is not None and not unlinked(result):
                raise AssertionError("An external person of another organization reached A's records.")
    if disabled is not None:
        with disabled(), acting_external(scenario.external, scenario.a) as actor:
            try:
                call(actor)
            except ServiceError:
                pass
            else:
                raise AssertionError("The method answered with its app turned off.")
