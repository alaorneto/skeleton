"""Admission policy and join requests with approval (#26)."""

import json
import threading
from uuid import uuid4

import pytest
from django.core import mail
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy import admission
from skeleton.tenancy.context import request_context, system_context, tenant_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Join-requests-Password!26"


@pytest.fixture(autouse=True)
def clean_rate_limit():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def identity(email, *, verified=True):
    from allauth.account.models import EmailAddress

    with system_context(reason="test:join-identity"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=email, verified=verified, primary=True)
    return user


def member_with_role(owner, organization, email, code):
    from skeleton.access.models import MemberRole, Role
    from skeleton.tenancy.models import Membership

    user = identity(email)
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:join-role"):
        membership = Membership.objects.create(organization=organization, user=user)
        role = Role.objects.get(code=code, is_builtin=True)
        MemberRole.objects.create(organization=organization, membership=membership, role=role)
    return user


def login(user):
    client = Client()
    assert client.post(reverse("account_login"), {"login": user.email, "password": PASSWORD}).status_code == 302
    return client


def demote(user):
    with system_context(reason="test:join-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


@pytest.fixture
def orgs():
    owner_a, org_a = bootstrap_identity(
        email="join-owner-a@example.test", organization="join-a", name="Join A", password=PASSWORD
    )
    owner_b, org_b = bootstrap_identity(
        email="join-owner-b@example.test", organization="join-b", name="Join B", password=PASSWORD
    )
    demote(owner_a)
    demote(owner_b)
    return {"owner_a": owner_a, "a": org_a, "owner_b": owner_b, "b": org_b}


def open_requests(owner, organization, *, discoverable=True, role=None, revision=0):
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:join-policy"):
        data = {"discoverable": discoverable, "join_requests_enabled": True}
        if role is not None:
            data["request_default_role"] = role
        return admission.configure_policy(actor=owner, data=data, expected_revision=revision)


def submit(user, organization, message=""):
    with request_context(actor=user, request_id=uuid4(), source="test:join-submit"):
        return admission.submit_join_request(user=user, organization_id=organization.pk, message=message)


def memberships(owner, organization, user):
    from skeleton.access.models import MemberRole
    from skeleton.tenancy.models import Membership

    with tenant_context(organization_id=organization.pk, actor=owner, source="test:join-check"):
        rows = list(Membership.objects.filter(user=user, is_active=True))
        roles = list(MemberRole.objects.filter(membership__in=rows).values_list("role__code", flat=True))
    return rows, roles


def decide(actor, organization, record, *, approve=True, revision=None):
    with tenant_context(organization_id=organization.pk, actor=actor, source="test:join-decide"):
        return admission.decide_join_request(
            actor=actor,
            join_request_id=record.pk,
            approve=approve,
            expected_revision=record.revision if revision is None else revision,
        )


def test_without_policy_nothing_is_listed_and_requests_are_neutral_404(orgs):
    person = identity("join-nobody@example.test")
    client = login(person)
    page = client.get(reverse("web:join"))
    assert page.status_code == 200 and list(page.context["organizations"]) == []
    for organization_id in (orgs["a"].pk, uuid4()):
        url = reverse("web:join-organization", args=[organization_id])
        assert client.get(url).status_code == 404
        assert client.post(url, {"message": "hi"}).status_code == 404
    api = client.post(
        reverse("api:me-join-requests"),
        json.dumps({"organization_id": str(orgs["a"].pk)}),
        content_type="application/json",
    )
    assert api.status_code == 404 and api.json()["code"] == "not_found"
    # Accepting but not discoverable: reachable by address only, never listed.
    open_requests(orgs["owner_a"], orgs["a"], discoverable=False)
    assert list(client.get(reverse("web:join")).context["organizations"]) == []
    assert client.get(reverse("web:join-organization", args=[orgs["a"].pk])).status_code == 200


def test_unverified_email_cannot_ask(orgs):
    open_requests(orgs["owner_a"], orgs["a"])
    person = identity("join-unverified@example.test", verified=False)
    with pytest.raises(PermissionDenied):
        submit(person, orgs["a"])
    # allauth does not open a session for an unverified account; the services refuse it anyway.
    assert admission.directory(person) == ()


def test_full_flow_is_idempotent_and_approval_creates_one_membership(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    open_requests(owner, a)
    person = identity("join-person@example.test")
    client = login(person)
    listed = client.get(reverse("web:join")).context["organizations"]
    assert [item.organization_id for item in listed] == [a.pk]
    url = reverse("web:join-organization", args=[a.pk])
    assert client.post(url, {"message": "Sou da equipe de vendas."}).status_code == 302
    assert client.post(url, {"message": "De novo"}).status_code == 302
    assert list(client.get(reverse("web:join")).context["organizations"]) == []  # Already asked.
    with request_context(actor=person, request_id=uuid4(), source="test:join-mine"):
        mine = admission.my_join_requests(person)
    assert len(mine) == 1 and mine[0].message == "Sou da equipe de vendas." and mine[0].status == "pending"

    approver = login(owner)
    queue = approver.get(reverse("web:join-requests", args=[a.pk]))
    assert queue.status_code == 200 and person.email.encode() in queue.content
    [row] = queue.context["rows"]
    assert row["returning"] is False
    decided = approver.post(
        reverse("web:join-request-decide", args=[a.pk, row["record"].pk]),
        {"decision": "approve", "revision": row["record"].revision},
    )
    assert decided.status_code == 302
    rows, roles = memberships(owner, a, person)
    assert len(rows) == 1 and roles == ["member"]
    assert client.get(reverse("web:dashboard", args=[a.pk])).status_code == 200
    assert [message.to for message in mail.outbox][-1] == [person.email]
    assert "aprovado" in mail.outbox[-1].body
    # Already a member: the form sends the person to the organization, nothing new is created.
    assert client.post(url, {}).status_code in (302, 404)


def test_two_decisions_on_the_same_revision_have_one_winner(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    open_requests(owner, a)
    people = member_with_role(owner, a, "join-people@example.test", "people_manager")
    person = identity("join-race@example.test")
    record = submit(person, a)
    decide(owner, a, record, approve=False)
    with pytest.raises(ConcurrentUpdate):
        decide(people, a, record, approve=True)
    rows, _ = memberships(owner, a, person)
    assert rows == []
    client = login(people)
    stale = client.post(
        reverse("web:join-request-decide", args=[a.pk, record.pk]), {"decision": "approve", "revision": 1}
    )
    assert stale.status_code == 409


def test_concurrent_decisions_on_postgres_have_one_winner(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    open_requests(owner, a)
    people = member_with_role(owner, a, "join-people-race@example.test", "people_manager")
    person = identity("join-threads@example.test")
    record = submit(person, a)
    barrier = threading.Barrier(2)
    results = []

    def run(actor, approve):
        try:
            barrier.wait()
            decide(actor, a, record, approve=approve)
            results.append("ok")
        except ConcurrentUpdate:
            results.append("conflict")
        except Exception as error:  # noqa: BLE001
            results.append(type(error).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(owner, True)), threading.Thread(target=run, args=(people, False))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["conflict", "ok"], results


def test_people_manager_cannot_approve_into_a_role_with_capabilities(orgs):
    from skeleton.access.models import Role

    owner, a = orgs["owner_a"], orgs["a"]
    with tenant_context(organization_id=a.pk, actor=owner, source="test:join-role-pick"):
        app_manager = Role.objects.get(code="app_manager", is_builtin=True)
        admin_role = Role.objects.get(code="organization_admin", is_builtin=True)
        with pytest.raises(ValidationError):
            admission.configure_policy(
                actor=owner,
                data={"join_requests_enabled": True, "request_default_role": admin_role},
                expected_revision=0,
            )
    open_requests(owner, a, role=app_manager)
    people = member_with_role(owner, a, "join-people-priv@example.test", "people_manager")
    person = identity("join-priv@example.test")
    record = submit(person, a)
    with pytest.raises(PermissionDenied):
        decide(people, a, record)
    assert memberships(owner, a, person)[0] == []
    client = login(people)
    response = client.post(
        reverse("web:join-request-decide", args=[a.pk, record.pk]), {"decision": "approve", "revision": record.revision}
    )
    assert response.status_code == 403
    decide(owner, a, record)
    assert memberships(owner, a, person)[1] == ["app_manager"]


def test_requester_sees_own_requests_in_a_and_b_and_approvers_see_only_theirs(orgs):
    from skeleton.tenancy.models import JoinRequest

    open_requests(orgs["owner_a"], orgs["a"])
    open_requests(orgs["owner_b"], orgs["b"])
    person = identity("join-both@example.test")
    other = identity("join-other@example.test")
    submit(person, orgs["a"])
    submit(person, orgs["b"])
    submit(other, orgs["b"])
    with request_context(actor=person, request_id=uuid4(), source="test:join-own"):
        assert {item.organization_id for item in admission.my_join_requests(person)} == {orgs["a"].pk, orgs["b"].pk}
    api = login(person).get(reverse("api:me-join-requests")).json()["results"]
    assert {item["organization_id"] for item in api} == {str(orgs["a"].pk), str(orgs["b"].pk)}
    assert all("role" not in item and "user" not in item for item in api)
    with tenant_context(organization_id=orgs["a"].pk, actor=orgs["owner_a"], source="test:join-a-sees"):
        assert JoinRequest.objects.count() == 1
        assert JoinRequest.all_objects.count() == 1
        assert admission.pending_requests().count() == 1
    client = login(orgs["owner_a"])
    listing = client.get(reverse("api:organization-join-requests", args=[orgs["a"].pk])).json()["results"]
    assert [item["user"]["email"] for item in listing] == [person.email]
    with tenant_context(organization_id=orgs["b"].pk, actor=orgs["owner_b"], source="test:join-b-id"):
        foreign = JoinRequest.objects.get(user=other)
    assert client.get(reverse("api:organization-join-request", args=[orgs["a"].pk, foreign.pk])).status_code == 404
    response = client.post(
        reverse("api:organization-join-request", args=[orgs["a"].pk, foreign.pk]),
        json.dumps({"decision": "approve"}),
        content_type="application/json",
        headers={"If-Match": f'"{foreign.revision}"'},
    )
    assert response.status_code == 404
    assert client.get(reverse("web:join-requests", args=[orgs["b"].pk])).status_code == 404
    # A plain member of A cannot see the queue; the people manager can.
    member = member_with_role(orgs["owner_a"], orgs["a"], "join-plain@example.test", "member")
    assert login(member).get(reverse("web:join-requests", args=[orgs["a"].pk])).status_code == 403


def test_api_decision_requires_if_match_and_refuses_mass_assignment(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    open_requests(owner, a)
    person = identity("join-api@example.test")
    record = submit(person, a)
    client = login(owner)
    url = reverse("api:organization-join-request", args=[a.pk, record.pk])
    body = json.dumps({"decision": "approve"})
    assert client.post(url, body, content_type="application/json").status_code == 428
    for extra in ({"decision": "approve", "status": "approved"}, {"decision": "approve", "role": str(uuid4())}):
        bad = client.post(url, json.dumps(extra), content_type="application/json", headers={"If-Match": '"1"'})
        assert bad.status_code == 400
    stale = client.post(url, body, content_type="application/json", headers={"If-Match": '"9"'})
    assert stale.status_code == 412
    done = client.post(url, body, content_type="application/json", headers={"If-Match": f'"{record.revision}"'})
    assert done.status_code == 200 and done.json()["status"] == "approved"
    again = client.post(url, body, content_type="application/json", headers={"If-Match": f'"{record.revision + 1}"'})
    assert again.status_code in (404, 409)
    mine = login(person).post(
        reverse("api:me-join-requests"),
        json.dumps({"organization_id": str(a.pk), "status": "approved"}),
        content_type="application/json",
    )
    assert mine.status_code == 400


def test_withdraw_and_expiry(orgs, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    owner, a = orgs["owner_a"], orgs["a"]
    open_requests(owner, a)
    person = identity("join-withdraw@example.test")
    record = submit(person, a)
    client = login(person)
    url = reverse("web:join-withdraw", args=[record.pk])
    assert client.post(url, {"revision": record.revision + 1}).status_code == 409
    assert client.post(url, {"revision": record.revision}).status_code == 302
    with request_context(actor=person, request_id=uuid4(), source="test:join-after-withdraw"):
        assert admission.my_join_requests(person)[0].status == "withdrawn"
    # A stranger cannot withdraw someone else's request: it does not exist for them.
    stranger = login(identity("join-stranger@example.test"))
    second = submit(person, a)
    assert stranger.post(reverse("web:join-withdraw", args=[second.pk]), {"revision": 1}).status_code == 404
    # Expiry is derived: past 30 days the request cannot be decided and a new one replaces it.
    later = timezone.now() + timedelta(days=31)
    monkeypatch.setattr(admission.timezone, "now", lambda: later)
    with pytest.raises(ValidationError):
        decide(owner, a, second)
    third = submit(person, a)
    assert third.pk != second.pk
    with request_context(actor=person, request_id=uuid4(), source="test:join-expired"):
        statuses = sorted(item.status for item in admission.my_join_requests(person))
    assert statuses == ["expired", "pending", "withdrawn"]


def test_rate_limit_answers_429_without_revealing_the_organization(orgs, monkeypatch):
    monkeypatch.setattr(admission, "RATE_LIMIT", 2)
    person = identity("join-rate@example.test")
    client = login(person)
    for organization_id in (uuid4(), uuid4()):
        assert client.post(reverse("web:join-organization", args=[organization_id]), {}).status_code == 404
    for organization_id in (uuid4(), orgs["a"].pk):
        response = client.post(reverse("web:join-organization", args=[organization_id]), {})
        assert response.status_code == 429 and int(response["Retry-After"]) > 0


def test_approval_reactivates_a_previous_membership_and_warns(orgs):
    from skeleton.access import services as access_services

    owner, a = orgs["owner_a"], orgs["a"]
    open_requests(owner, a)
    person = member_with_role(owner, a, "join-returning@example.test", "member")
    with tenant_context(organization_id=a.pk, actor=person, source="test:join-leave"):
        access_services.leave_organization(person)
    record = submit(person, a)
    queue = login(owner).get(reverse("web:join-requests", args=[a.pk]))
    assert queue.context["rows"][0]["returning"] is True
    assert "Já foi membro" in queue.content.decode()
    decide(owner, a, record)
    rows, roles = memberships(owner, a, person)
    assert len(rows) == 1 and roles == ["member"]


def test_policy_cas_audit_and_neutral_tables(orgs):
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.models import OrganizationAdmissionPolicy

    owner, a = orgs["owner_a"], orgs["a"]
    record = open_requests(owner, a)
    with pytest.raises(ConcurrentUpdate):
        open_requests(owner, a, revision=0)
    open_requests(owner, a, discoverable=False, revision=record.revision)
    with tenant_context(organization_id=a.pk, actor=owner, source="test:join-audit"):
        policy = OrganizationAdmissionPolicy.objects.get()
        operations = list(
            AuditEvent.objects.filter(record_sysid=policy.pk)
            .order_by("record_revision")
            .values_list("operation", flat=True)
        )
    assert operations == ["create", "update"]
    client = login(owner)
    for key in ("sk_tenancy.joinrequest", "sk_tenancy.organizationadmissionpolicy"):
        records = reverse("api:records", kwargs={"organization_id": a.pk, "table_key": key})
        assert client.get(records).status_code in (403, 404)
    # The policy page needs admission.configure: a people manager gets 404, a member 403.
    people = member_with_role(owner, a, "join-people-policy@example.test", "people_manager")
    assert login(people).get(reverse("web:admission", args=[a.pk])).status_code == 404
    assert client.get(reverse("web:admission", args=[a.pk])).status_code == 200
    stale = client.post(
        reverse("web:admission", args=[a.pk]), {"join_requests_enabled": "on", "revision": record.revision}
    )
    assert stale.status_code == 409
