"""Organization member limits of the core (#695), with billing off.

Runs on PostgreSQL with the runtime role. The instance default (``SKELETON_ORGANIZATION_MEMBER_LIMIT``)
is checked on every path that creates, restores or reactivates a membership; service accounts
never take a seat; organization B is never affected by A.
"""

import pytest

from skeleton.common.exceptions import QuotaLimitExceeded
from skeleton.tenancy.context import system_context
from skeleton.testing.people import add_member, make_organization, make_person
from skeleton.testing.service_api import acting_as

PASSWORD = "Limits-fixture-2026!"


@pytest.fixture
def people(skeleton_db):
    persons = {
        name: make_person(email=f"{name}@org-limits.example.test", password=PASSWORD)
        for name in ("owner-a", "owner-b", "ana", "bia", "caio", "duda")
    }
    a = make_organization(owner=persons["owner-a"], slug="org-limits-a", name="Limits A")
    b = make_organization(owner=persons["owner-b"], slug="org-limits-b", name="Limits B")
    return persons, a, b


def _status(organization, metric="members"):
    from skeleton.system.limits import limit_status

    with system_context(reason="test:limits-status", organization_id=organization.pk):
        return limit_status(metric)


def test_without_a_default_members_stay_unlimited(people):
    persons, a, _ = people
    for name in ("ana", "bia", "caio", "duda"):
        add_member(organization=a, user=persons[name])
    status = _status(a)
    assert (status.used, status.limit, status.base, status.contributions) == (5, None, None, ())
    assert not status.over_limit and status.remaining is None


def test_the_default_refuses_admission_creation_restoration_and_reactivation(people, settings):
    from skeleton.access.services import create_membership, delete_membership, update_membership
    from skeleton.tenancy.models import Membership

    persons, a, _ = people
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 2
    ana = add_member(organization=a, user=persons["ana"])  # The owner and Ana: full.
    with pytest.raises(QuotaLimitExceeded) as refused:
        add_member(organization=a, user=persons["bia"])  # admit_member (invitation, SSO, join).
    assert (refused.value.metric, refused.value.limit) == ("members", 2)
    with acting_as(persons["owner-a"], a) as owner:
        with pytest.raises(QuotaLimitExceeded):
            create_membership(user=owner, data={"user": persons["bia"]})  # A new membership.
        create_membership(user=owner, data={"user": persons["bia"], "is_active": False})  # Inactive: no seat.
        delete_membership(Membership.objects.get(pk=ana.pk), user=owner)  # Frees Ana's seat.
        bia = Membership.objects.get(user=persons["bia"])
        update_membership(bia, user=owner, data={"is_active": True})  # Takes the freed seat.
        with pytest.raises(QuotaLimitExceeded):
            create_membership(user=owner, data={"user": persons["ana"]})  # Restoring an active one.
        update_membership(Membership.objects.get(pk=bia.pk), user=owner, data={"is_active": False})
        create_membership(user=owner, data={"user": persons["ana"]})  # The seat is free again: restored.
        with pytest.raises(QuotaLimitExceeded):
            update_membership(Membership.objects.get(pk=bia.pk), user=owner, data={"is_active": True})
        assert Membership.objects.get(pk=ana.pk).is_active  # Same row, same sysid.
    assert _status(a).used == 2


def test_service_accounts_never_take_a_seat(people, settings):
    from skeleton.accounts.services import create_service_account

    persons, a, _ = people
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    with acting_as(persons["owner-a"], a) as owner:
        create_service_account(actor=owner, name="Integração")
    status = _status(a)
    assert (status.used, status.limit, status.remaining) == (1, 1, 0)


def test_a_lower_limit_removes_nobody_and_reports_over_limit(people, settings):
    persons, a, _ = people
    for name in ("ana", "bia"):
        add_member(organization=a, user=persons[name])
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 2
    status = _status(a)
    assert (status.used, status.limit, status.base, status.over_limit, status.remaining) == (3, 2, 2, True, 0)
    with pytest.raises(QuotaLimitExceeded):
        add_member(organization=a, user=persons["caio"])


def test_organizations_are_isolated(people, settings):
    from skeleton.system.limits import check_limit

    persons, a, b = people
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 2
    add_member(organization=a, user=persons["ana"])  # A is full.
    add_member(organization=b, user=persons["bia"])  # B is not affected by A.
    with system_context(reason="test:limits-b", organization_id=b.pk):
        check_limit(a.pk, "members", 1)  # A's rows are not readable from B: nothing is checked.
    assert (_status(a).used, _status(b).used) == (2, 2)


def test_unknown_metric_and_zero_increment(people, settings):
    from skeleton.system.limits import check_limit, limit_status

    _, a, _ = people
    settings.SKELETON_ORGANIZATION_MEMBER_LIMIT = 1
    with system_context(reason="test:limits-misc", organization_id=a.pk):
        check_limit(a.pk, "members", 0)  # Nothing to add: nothing checked, even when full.
        with pytest.raises(ValueError, match="Unknown limit metric"):
            check_limit(a.pk, "acme.unknown", 1)
        with pytest.raises(ValueError, match="Unknown limit metric"):
            limit_status("acme.unknown")
        assert limit_status("external_people").limit is None  # No default for external people.
