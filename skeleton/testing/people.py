"""People, organizations, memberships, roles and groups for the tests of an app (#572).

The tests of an app in its own repository build their scenarios only with the public surfaces of
the core: W019 points at any other import, test modules included. These helpers go through the
same provisioning services as ``build_service_scenario`` (``create_identity``,
``create_organization``, ``provision_membership``, ``create_role``, ``create_group`` and
``add_group_member``), each in the system context of a fixture, and return the record created:

- ``make_person(email=...)``: an active identity with its email verified;
- ``make_organization(owner=..., slug=..., name=...)``: an organization whose owner is its administrator;
- ``make_role(organization=..., code=..., name=...)``: a custom role of the organization;
- ``add_member(organization=..., user=..., role_code="member")``: links the person with a role
  (builtin or custom); calling it again with another role adds that role;
- ``make_group(organization=..., name=..., members=..., admin=None)``: a group with those members,
  created by ``admin`` (as an administrator would, with the same audit) or by the fixture.

Table permissions of a role come from ``skeleton.testing.applications.grant_table_actions``.
Tests only; use them inside the isolated test database fixture of the project, never against a
shared database.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from typing import Any

__all__ = ["FIXTURE_PASSWORD", "add_member", "make_group", "make_organization", "make_person", "make_role"]

# Drawn once per process (#321): no literal password in the kit.
FIXTURE_PASSWORD = secrets.token_urlsafe(24)


def make_person(*, email: str, password: str | None = None, first_name: str = "", last_name: str = "") -> Any:
    """Create an active identity with ``email`` verified and primary; ``FIXTURE_PASSWORD`` by default."""
    from allauth.account.models import EmailAddress

    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.context import system_context

    with system_context(reason="fixture:testing-people-identity"):
        user = create_identity(
            email=email,
            password=FIXTURE_PASSWORD if password is None else password,
            first_name=first_name,
            last_name=last_name,
        )
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    return user


def make_organization(*, owner: Any, slug: str, name: str) -> Any:
    """Create an organization whose administrator is ``owner`` (an identity from ``make_person``)."""
    from skeleton.tenancy.services import create_organization

    return create_organization(owner=owner, slug=slug, name=name)


def make_role(*, organization: Any, code: str, name: str, description: str = "") -> Any:
    """Create a custom role in ``organization``; builtin codes (``member``...) are refused by the service."""
    from skeleton.access.services import create_role
    from skeleton.tenancy.context import system_context

    with system_context(reason="fixture:testing-people-role", organization_id=organization.pk):
        return create_role(user=None, data={"code": code, "name": name, "description": description})


def add_member(*, organization: Any, user: Any, role_code: str = "member") -> Any:
    """Link ``user`` to ``organization`` with the role ``role_code`` and return the membership.

    The role is builtin (``member``, ``organization_admin``...) or created by ``make_role``. A
    person already linked keeps the membership and receives one more role.
    """
    from skeleton.access.models import Role
    from skeleton.access.services import provision_membership
    from skeleton.tenancy.context import system_context

    with system_context(reason="fixture:testing-people-membership", organization_id=organization.pk):
        role = Role.objects.filter(organization=organization, code=role_code).first()
        if role is None:
            raise LookupError(f"The organization has no role {role_code!r}: create it with make_role first.")
        return provision_membership(organization=organization, user=user, role=role)


def make_group(
    *,
    organization: Any,
    name: str,
    members: Iterable[Any] = (),
    code: str | None = None,
    admin: Any | None = None,
) -> Any:
    """Create a group in ``organization`` with ``members`` (people already linked to it) and return it.

    With ``admin``, the group is created as that administrator would create it (``access.manage``
    and its audit); without it, in the system context of a fixture. ``code`` defaults to the
    slug of ``name``.
    """
    from django.utils.text import slugify

    from skeleton.access.services import add_group_member, create_group
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Membership

    from .service_api import acting_as

    people = list(members)
    data = {"code": code or slugify(name)[:50] or "group", "name": name}
    context = (
        acting_as(admin, organization)
        if admin is not None
        else system_context(reason="fixture:testing-people-group", organization_id=organization.pk)
    )
    with context:
        memberships = []
        for person in people:
            membership = Membership.objects.filter(
                organization=organization, user_id=getattr(person, "pk", person), is_active=True
            ).first()
            if membership is None:
                raise LookupError("A group member must be an active member of the organization: use add_member first.")
            memberships.append(membership)
        group = create_group(user=admin, data=data)
        for membership in memberships:
            add_group_member(user=admin, data={"group": group, "membership": membership})
    return group
