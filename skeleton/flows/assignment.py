"""Resolve the declared assignment of a step to people and queues of the current organization.

A definition is shared by every organization, so it names roles and groups by code and
people by where they come from (starter, subject owner, a form field, a rule of the app).
Resolution happens when the step is activated; only active memberships of people count,
and a rule's answer is checked against the tenant like any other input.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .errors import FlowFailure
from .registry import Assignees, get_assignment
from .spec import Assignment


@dataclass(frozen=True)
class Slot:
    """One task to open: a person, or a queue of a group or of a role."""

    user: UUID | None = None
    group: UUID | None = None
    role: UUID | None = None

    def as_json(self) -> dict[str, str]:
        return {name: str(value) for name in ("user", "group", "role") if (value := getattr(self, name))}

    @classmethod
    def from_json(cls, data: dict[str, str]) -> Slot:
        return cls(**{name: UUID(value) for name, value in data.items()})


def active_people(user_ids: Iterable[Any]) -> list[UUID]:
    """The given users that are people with an active membership here, in a stable order."""
    from skeleton.accounts.models import AccountType
    from skeleton.tenancy.models import Membership

    wanted = [UUID(str(item)) for item in user_ids if item]
    found = set(
        Membership.objects.filter(
            user_id__in=wanted,
            is_active=True,
            user__is_active=True,
            user__deleted_at__isnull=True,
            user__account_type=AccountType.HUMAN,
        ).values_list("user_id", flat=True)
    )
    return [item for item in dict.fromkeys(wanted) if item in found]


def _group_people(group_id: UUID) -> list[UUID]:
    from skeleton.access.models import GroupMember

    rows = GroupMember.objects.filter(group_id=group_id).order_by("membership__user_id")
    return active_people(rows.values_list("membership__user_id", flat=True))


def _role_people(role_id: UUID) -> list[UUID]:
    from skeleton.access.models import GroupMember, GroupRole, MemberRole

    direct = MemberRole.objects.filter(role_id=role_id).values_list("membership__user_id", flat=True)
    groups = GroupRole.objects.filter(role_id=role_id, group__deleted_at__isnull=True).values_list(
        "group_id", flat=True
    )
    grouped = GroupMember.objects.filter(group_id__in=groups).values_list("membership__user_id", flat=True)
    return active_people(sorted({*direct, *grouped}, key=str))


def _subject_owner(execution: Any) -> Any:
    from skeleton.system.registry import get_spec

    try:
        model = get_spec(execution.instance.subject_table).model
    except KeyError:
        return None
    return model.objects.filter(pk=execution.instance.subject_sysid).values_list("owner_id", flat=True).first()


def resolve(execution: Any, step_key: str, assignments: Iterable[Assignment]) -> list[Slot]:
    """The slots of a step, without duplicates; an assignment that resolves to nobody adds nothing."""
    from skeleton.access.models import AccessGroup, Role

    slots: list[Slot] = []

    def people(ids: Iterable[Any]) -> None:
        slots.extend(Slot(user=item) for item in active_people(ids))

    for item in assignments:
        if item.kind == "starter":
            people([execution.instance.starter_id])
        elif item.kind == "subject_owner":
            people([_subject_owner(execution)])
        elif item.kind == "field":
            try:
                people([execution.instance.data.get(item.value)])
            except ValueError:
                continue
        elif item.kind in ("role", "group"):
            model = Role if item.kind == "role" else AccessGroup
            target = model.objects.filter(code=item.value).values_list("pk", flat=True).first()
            if target is None:
                continue
            if item.expand:
                people(_role_people(target) if item.kind == "role" else _group_people(target))
            else:
                slots.append(Slot(role=target) if item.kind == "role" else Slot(group=target))
        elif item.kind == "rule":
            try:
                entry = get_assignment(item.value)
            except KeyError:
                raise FlowFailure("assignment_rule_missing", step_key) from None
            if entry.owner_app != execution.spec.owner_app:
                raise FlowFailure("assignment_rule_missing", step_key)
            answer = entry.function(execution.context(step_key))
            if not isinstance(answer, Assignees):
                raise FlowFailure("assignment_rule_result", step_key)
            people(answer.users)
            # Tenant-filtered managers: an identifier of another organization is not found.
            slots.extend(
                Slot(group=pk) for pk in AccessGroup.objects.filter(pk__in=answer.groups).values_list("pk", flat=True)
            )
            slots.extend(Slot(role=pk) for pk in Role.objects.filter(pk__in=answer.roles).values_list("pk", flat=True))
    return list(dict.fromkeys(slots))
