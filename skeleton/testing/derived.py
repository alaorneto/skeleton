"""Contract of a derived table of an app (#566): access decided by the anchor, proved on the runtime role.

A derived table declares ``TableSpec.access_parent`` and is authorized through the exposed
``SecuredRecord`` at the end of the chain, its anchor (docs/access-control.md, "Tabelas
derivadas"). ``assert_derived_access_contract`` proves, collecting every problem at once:

- without a grant on a private anchor, the child is invisible;
- a read grant on the anchor reads the child and never writes it;
- an update grant on the anchor writes the child;
- a child of another organization is invisible;
- a child whose anchor has a tombstone is invisible, even for ``restore``;
- without the table permission of the anchor, the child is refused, even to the anchor's owner;
- with the app disabled, the child is refused;
- ``manage_access`` on the child is always refused.

``grant_anchor`` and ``create_group`` prepare grants and groups through the core access services,
as an administrator would, so the tests of an app never import ``skeleton.access.models``. Tests
only; use them inside the isolated test database fixture of the project.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any
from uuid import UUID

from .service_api import ServiceScenario, acting_as

__all__ = ["assert_derived_access_contract", "create_group", "grant_anchor"]


def _side(scenario: ServiceScenario, organization_id: Any) -> tuple[Any, Any]:
    """The organization of the scenario with that id and its administrator."""
    if organization_id == scenario.a.pk:
        return scenario.a, scenario.admin_a
    if organization_id == scenario.b.pk:
        return scenario.b, scenario.admin_b
    raise ValueError("The record belongs to neither organization of the scenario.")


def grant_anchor(
    scenario: ServiceScenario, anchor: Any, *, action: str, user: Any | None = None, group: UUID | None = None
) -> None:
    """Share ``action`` (read, update or delete) on ``anchor`` with a person or a group.

    Goes through ``AccessService.grant_record_access`` as the administrator of the anchor's
    organization: the same path and audit as the access management pages.
    """
    from skeleton.service_api.access import AccessService

    if (user is None) == (group is None):
        raise TypeError("Pass exactly one of user or group.")
    organization, admin = _side(scenario, anchor.organization_id)
    with acting_as(admin, organization) as actor:
        AccessService().grant_record_access(
            actor=actor,
            table_key=type(anchor)._meta.label_lower,
            record_id=anchor.pk,
            action=action,
            group_id=group,
            membership_user_id=getattr(user, "pk", user),
        )


def create_group(
    scenario: ServiceScenario, name: str, members: Iterable[Any], *, organization: Any | None = None
) -> UUID:
    """Create a group in organization A (or ``organization``) with ``members``; return its sysid."""
    from django.utils.text import slugify

    from skeleton.access.services import add_group_member
    from skeleton.access.services import create_group as create_access_group
    from skeleton.tenancy.models import Membership

    organization, admin = _side(scenario, (organization or scenario.a).pk)
    with acting_as(admin, organization) as actor:
        group = create_access_group(user=actor, data={"code": slugify(name)[:50] or "group", "name": name})
        for member in members:
            membership = Membership.objects.get(user_id=getattr(member, "pk", member), is_active=True)
            add_group_member(user=actor, data={"group": group, "membership": membership})
    return group.pk


def _tombstone(record: Any) -> None:
    """Mark a parent deleted although live children reference it, as only SQL can (a defensive invariant)."""
    from django.db import connection

    from skeleton.tenancy.context import system_context

    table = connection.ops.quote_name(type(record)._meta.db_table)
    with system_context(reason="fixture:derived-contract-tombstone", organization_id=record.organization_id):
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE skeleton.{table} SET deleted_at = now(), revision = revision + 1 WHERE sysid = %s",  # noqa: S608 - quoted identifier
                [record.pk],
            )


def assert_derived_access_contract(
    *,
    child_model: Any,
    make_anchor: Callable[..., Any],
    make_child: Callable[[Any], Any],
    scenario: ServiceScenario,
) -> None:
    """Fail with every problem of the derived access contract of ``child_model`` at once.

    ``scenario`` comes from ``build_service_scenario`` (of the child or of its anchor table): its
    ``member`` holds every table action on the anchor table. ``make_anchor(organization=..., owner=...)``
    creates an anchor record with ``visibility="private"`` and returns it; ``make_child(anchor)``
    creates a row of ``child_model`` under that anchor (creating the intermediate rows of a longer
    chain) and returns it. Both run in a fixture system context of the anchor's organization.
    """
    from django.core.exceptions import PermissionDenied

    from skeleton.access.derivation import is_derived
    from skeleton.access.policies import authorized_queryset, require_record_action
    from skeleton.tenancy.context import system_context

    problems: list[str] = []
    if not is_derived(child_model):
        raise AssertionError(f"{child_model._meta.label} is not a valid derived table (see the E042 check).")

    def made(organization: Any, owner: Any) -> tuple[Any, Any]:
        with system_context(reason="fixture:derived-contract", organization_id=organization.pk):
            anchor = make_anchor(organization=organization, owner=owner)
            if getattr(anchor, "visibility", None) != "private":
                raise AssertionError("make_anchor must create an anchor with visibility='private'.")
            return anchor, make_child(anchor)

    def visible(user: Any, organization: Any, child: Any, action: str, **options: Any) -> bool | None:
        """Whether the child is in the user's rows; ``None`` when the whole table is refused."""
        with acting_as(user, organization):
            try:
                rows = authorized_queryset(child_model, user=user, action=action, **options)
                return rows.filter(pk=child.pk).exists()
            except PermissionDenied:
                return None

    member, a = scenario.member, scenario.a
    _private_anchor, hidden = made(a, scenario.admin_a)
    if visible(member, a, hidden, "read"):
        problems.append("Without a grant on a private anchor, the child is visible.")
    read_anchor, readable = made(a, scenario.admin_a)
    grant_anchor(scenario, read_anchor, user=member, action="read")
    if not visible(member, a, readable, "read"):
        problems.append("A read grant on the anchor does not read the child.")
    if visible(member, a, readable, "update"):
        problems.append("A read grant on the anchor writes the child.")
    write_anchor, writable = made(a, scenario.admin_a)
    grant_anchor(scenario, write_anchor, user=member, action="read")
    grant_anchor(scenario, write_anchor, user=member, action="update")
    if not visible(member, a, writable, "update"):
        problems.append("An update grant on the anchor does not write the child.")
    else:
        with acting_as(member, a):
            try:
                require_record_action(writable, user=member, action="update")
            except PermissionDenied:
                problems.append("require_record_action refuses the child that authorized_queryset allows.")
    _foreign_anchor, foreign = made(scenario.b, member)
    if visible(member, a, foreign, "read"):
        problems.append("A child of another organization is visible.")
    gone_anchor, orphan = made(a, member)
    if not visible(member, a, orphan, "read"):
        problems.append("The child of an anchor the member owns is not visible.")
    _tombstone(gone_anchor)
    if visible(member, a, orphan, "read") or visible(member, a, orphan, "restore", include_deleted=True):
        problems.append("The child of an anchor with a tombstone is visible.")
    _owned_anchor, owned = made(a, scenario.denied)
    if visible(scenario.denied, a, owned, "read") is not None:
        problems.append("Without the table permission of the anchor, the child is not refused (even to its owner).")
    with scenario.application_disabled():
        if visible(member, a, writable, "read") is not None:
            problems.append("With the app disabled, the child is not refused.")
    if visible(scenario.admin_a, a, writable, "manage_access") is not None:
        problems.append("manage_access on the child is not refused.")
    if problems:
        raise AssertionError(
            f"{child_model._meta.label} violates the derived access contract:\n- " + "\n- ".join(problems)
        )
