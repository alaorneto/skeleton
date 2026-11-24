"""The organization's notices about its people (#786): joined and left."""

import pytest

from skeleton.notifications.models import NotificationRecipient
from skeleton.notifications.retention import forget_person
from skeleton.tenancy.context import system_context
from skeleton.tenancy.member_notices import MEMBER_JOINED, MEMBER_LEFT
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.people import add_member, make_organization, make_person

pytestmark = pytest.mark.usefixtures("skeleton_db")


def set_name(user, name):
    from skeleton.accounts.models import UserProfile

    with system_context(reason="test:member-notices-name"):
        profile = UserProfile.objects.get(user=user)
        profile.display_name = name
        profile.save(update_fields={"display_name"})


@pytest.fixture
def organizations():
    owner_a = make_person(email="notices-owner-a@example.test")
    owner_b = make_person(email="notices-owner-b@example.test")
    a = make_organization(owner=owner_a, slug="notices-a", name="Acme")
    b = make_organization(owner=owner_b, slug="notices-b", name="Beta")
    run_pending_jobs()
    return a, b, owner_a, owner_b


def rows(organization):
    with system_context(reason="test:member-notices-rows", organization_id=organization.pk):
        return sorted(NotificationRecipient.objects.values_list("user_id", "type_code", "title"))


def test_creating_an_organization_tells_nobody(organizations):
    a, b, _owner_a, _owner_b = organizations
    assert rows(a) == [] and rows(b) == []


def test_joining_tells_the_managers(organizations):
    a, b, owner_a, owner_b = organizations
    person = make_person(email="notices-ana@example.test")
    set_name(person, "Ana")
    add_member(organization=a, user=person)
    run_pending_jobs()
    assert rows(a) == [(owner_a.pk, MEMBER_JOINED, "Ana entrou em Acme")]  # Never the person.
    assert rows(b) == []  # Nothing about A ever reaches B.
    run_pending_jobs()
    assert len(rows(a)) == 1  # Running the jobs again notifies nobody twice.


def test_a_plain_member_is_not_told(organizations):
    a, _b, owner_a, _owner_b = organizations
    first = make_person(email="notices-first@example.test")
    add_member(organization=a, user=first)
    run_pending_jobs()
    second = make_person(email="notices-second@example.test")
    add_member(organization=a, user=second)
    run_pending_jobs()
    joined = [row for row in rows(a) if row[1] == MEMBER_JOINED]
    assert {row[0] for row in joined} == {owner_a.pk}  # Only who manages members, never the first member.


def test_leaving_tells_the_managers(organizations):
    from skeleton.access.services import leave_organization
    from skeleton.tenancy.context import tenant_context

    a, _b, owner_a, _owner_b = organizations
    person = make_person(email="notices-bia@example.test")
    set_name(person, "Bia")
    add_member(organization=a, user=person)
    run_pending_jobs()
    with tenant_context(organization_id=a.pk, actor=person, source="test:member-notices-leave"):
        leave_organization(person)
    run_pending_jobs()
    left = [row for row in rows(a) if row[1] == MEMBER_LEFT]
    assert left == [(owner_a.pk, MEMBER_LEFT, "Bia não faz mais parte de Acme")]


def test_turned_off_by_the_organization_nobody_is_told(organizations):
    from skeleton.notifications import organization_settings
    from skeleton.testing.service_api import acting_as

    a, _b, owner_a, _owner_b = organizations
    with acting_as(owner_a, a):
        organization_settings.set_enabled(actor=owner_a, type_code=MEMBER_JOINED, enabled=False, expected_revision=None)
    add_member(organization=a, user=make_person(email="notices-off@example.test"))
    run_pending_jobs()
    assert [row for row in rows(a) if row[1] == MEMBER_JOINED] == []


def test_erasing_the_person_removes_the_name(organizations):
    a, _b, owner_a, _owner_b = organizations
    person = make_person(email="notices-gone@example.test")
    set_name(person, "Carla")
    add_member(organization=a, user=person)
    run_pending_jobs()
    with system_context(reason="test:member-notices-forget", organization_id=a.pk):
        forget_person(person.pk)
    assert [row for row in rows(a) if row[1] == MEMBER_JOINED] == [
        (owner_a.pk, MEMBER_JOINED, "Pessoa entrou na organização")
    ]
