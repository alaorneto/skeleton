"""Shareable, revocable multi-use join links and signup through a link (#27)."""

import json
import re
import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.common.exceptions import ImmutableRecord
from skeleton.tenancy import admission, links
from skeleton.tenancy.context import request_context, system_context, tenant_context
from tests import support

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Join-links-Password!27"


@pytest.fixture(autouse=True)
def clean_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def identity(email):
    from allauth.account.models import EmailAddress

    with system_context(reason="test:link-identity"):
        user = create_identity(email=email, password=PASSWORD)
        EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


def login(user):
    return support.login(user, password=PASSWORD, csrf=False)


def demote(user):
    with system_context(reason="test:link-demote"):
        user.is_staff = user.is_superuser = False
        user.save(update_fields=["is_staff", "is_superuser"])


def role(owner, organization, code):
    from skeleton.access.models import Role

    with tenant_context(organization_id=organization.pk, actor=owner, source="test:link-role"):
        return Role.objects.get(code=code, is_builtin=True)


def member_with_role(owner, organization, email, code):
    from skeleton.access.models import MemberRole
    from skeleton.tenancy.models import Membership

    user = identity(email)
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:link-member"):
        membership = Membership.objects.create(organization=organization, user=user)
        MemberRole.objects.create(
            organization=organization, membership=membership, role=role(owner, organization, code)
        )
    return user


@pytest.fixture
def orgs():
    owner_a, org_a = bootstrap_identity(
        email="link-owner-a@example.test", organization="link-a", name="Link A", password=PASSWORD
    )
    owner_b, org_b = bootstrap_identity(
        email="link-owner-b@example.test", organization="link-b", name="Link B", password=PASSWORD
    )
    demote(owner_a)
    demote(owner_b)
    return {"owner_a": owner_a, "a": org_a, "owner_b": owner_b, "b": org_b}


def policy(owner, organization, *, links_on=True, signup=False, revision=None):
    with tenant_context(organization_id=organization.pk, actor=owner, source="test:link-policy"):
        current = admission.current_policy()
        return admission.configure_policy(
            actor=owner,
            data={"join_links_enabled": links_on, "signup_via_link_enabled": signup},
            expected_revision=admission.revision_of(current) if revision is None else revision,
        )


def create(owner, organization, *, mode="direct", max_uses=10, days=7, code="member", actor=None):
    chosen = role(owner, organization, code)
    with tenant_context(organization_id=organization.pk, actor=actor or owner, source="test:link-create"):
        return links.create_join_link(
            actor=actor or owner,
            label="Equipe",
            role=chosen,
            mode=mode,
            days=days,
            max_uses=max_uses,
        )


def link_url(organization, token):
    return reverse("web:join-link", kwargs={"organization_id": organization.pk, "token": token})


def active_members(owner, organization, user):
    from skeleton.access.models import MemberRole
    from skeleton.tenancy.models import Membership

    with tenant_context(organization_id=organization.pk, actor=owner, source="test:link-check"):
        rows = list(Membership.objects.filter(user=user, is_active=True))
        roles = sorted(MemberRole.objects.filter(membership__in=rows).values_list("role__code", flat=True))
    return rows, roles


def stored_link(owner, organization, link):
    from skeleton.tenancy.models import JoinLink

    with tenant_context(organization_id=organization.pk, actor=owner, source="test:link-row"):
        return JoinLink.all_objects.get(pk=link.pk)


def test_direct_link_admits_once_and_members_consume_no_use(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    policy(owner, a)
    link, token = create(owner, a, mode="direct")
    person = identity("link-direct@example.test")
    client = login(person)
    page = client.get(link_url(a, token))
    assert page.status_code == 200 and b"Entrar na organiza" in page.content
    session = dict(client.session.items())
    assert session[links.SESSION_KEY]["digest"] == link.token_digest and token not in json.dumps(session)
    assert client.post(link_url(a, token)).status_code == 302
    assert active_members(owner, a, person)[1] == ["member"]
    assert client.post(link_url(a, token)).status_code == 302  # Already a member: no new use.
    assert stored_link(owner, a, link).use_count == 1


def test_approval_link_and_former_members_create_requests(orgs):
    from skeleton.access import services as access_services

    owner, a = orgs["owner_a"], orgs["a"]
    policy(owner, a)
    _, approval_token = create(owner, a, mode="approval")
    person = identity("link-approval@example.test")
    client = login(person)
    assert client.post(link_url(a, approval_token)).status_code == 302
    assert active_members(owner, a, person)[0] == []
    with request_context(actor=person, request_id=uuid4(), source="test:link-mine"):
        [request] = admission.my_join_requests(person)
    assert (request.channel, request.status) == ("link", "pending")

    former = member_with_role(owner, a, "link-former@example.test", "member")
    with tenant_context(organization_id=a.pk, actor=former, source="test:link-leave"):
        access_services.leave_organization(former)
    _, direct_token = create(owner, a, mode="direct")
    assert login(former).post(link_url(a, direct_token)).status_code == 302
    assert active_members(owner, a, former)[0] == []  # Never readmitted automatically.
    with request_context(actor=former, request_id=uuid4(), source="test:link-former-mine"):
        assert [item.status for item in admission.my_join_requests(former)] == ["pending"]


def test_concurrent_redemptions_of_a_single_use_link_have_one_winner(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    policy(owner, a)
    link, token = create(owner, a, mode="direct", max_uses=1)
    proof = links.validate_join_link(a.pk, token)
    people = [identity(f"link-race-{index}@example.test") for index in range(4)]
    barrier = threading.Barrier(len(people))
    results = []

    def run(user):
        try:
            barrier.wait()
            with request_context(actor=user, request_id=uuid4(), source="test:link-race"):
                results.append(links.redeem_join_link(user=user, proof=proof).outcome)
        except links.InvalidJoinLink:
            results.append("invalid")
        except Exception as error:  # noqa: BLE001
            results.append(type(error).__name__)
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=(user,)) for user in people]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["admitted", "invalid", "invalid", "invalid"], results
    assert stored_link(owner, a, link).use_count == 1
    assert sum(bool(active_members(owner, a, user)[0]) for user in people) == 1


def test_expired_revoked_exhausted_and_foreign_links_look_the_same(orgs, monkeypatch):
    owner, a, b = orgs["owner_a"], orgs["a"], orgs["b"]
    policy(owner, a)
    policy(orgs["owner_b"], b)
    _, expired_token = create(owner, a, days=1)
    revoked, revoked_token = create(owner, a)
    with tenant_context(organization_id=a.pk, actor=owner, source="test:link-revoke"):
        with pytest.raises(Exception):  # noqa: B017 - stale revision
            links.revoke_join_link(actor=owner, link_id=revoked.pk, expected_revision=revoked.revision + 1)
        links.revoke_join_link(actor=owner, link_id=revoked.pk, expected_revision=revoked.revision)
    exhausted, exhausted_token = create(owner, a, max_uses=1)
    login(identity("link-first@example.test")).post(link_url(a, exhausted_token))
    _, good_token = create(owner, a)
    client = login(identity("link-late@example.test"))
    client.get(reverse("web:home"))  # Consume the login message so pages compare equal.
    later = timezone.now() + timedelta(days=2)
    with monkeypatch.context() as clock:
        clock.setattr(links.timezone, "now", lambda: later)
        expired_page = client.get(link_url(a, expired_token))
    pages = [
        expired_page,
        client.get(link_url(a, revoked_token)),
        client.get(link_url(a, exhausted_token)),
        client.get(link_url(b, good_token)),  # A's token in B's address.
        client.get(link_url(a, "x" * 43)),
    ]
    assert {page.status_code for page in pages} == {404}
    main = {re.search(rb"<main.*</main>", page.content, re.S).group(0) for page in pages}
    assert len(main) == 1
    # Turning links off closes the live ones too.
    policy(owner, a, links_on=False)
    assert client.get(link_url(a, good_token)).status_code == 404


def test_token_never_stored_logged_audited_or_listed(orgs, caplog):
    from skeleton.audit.models import AuditEvent

    owner, a = orgs["owner_a"], orgs["a"]
    policy(owner, a)
    client = login(owner)
    response = client.post(
        reverse("web:join-links", args=[a.pk]),
        {"label": "Turma", "role": str(role(owner, a, "member").pk), "mode": "approval", "days": 7, "max_uses": 5},
    )
    assert response.status_code == 201
    url = response.context["created"]["url"]
    token = re.search(r"/join/[0-9a-f-]+/([^/]+)/$", url).group(1)
    assert "no-cache" in response["Cache-Control"] or "no-store" in response["Cache-Control"]
    assert token.encode() not in client.get(reverse("web:join-links", args=[a.pk])).content
    listing = client.get(reverse("api:organization-join-links", args=[a.pk]))
    assert listing.status_code == 200 and token not in listing.content.decode()
    assert all("digest" not in json.dumps(item) for item in listing.json()["results"])
    with tenant_context(organization_id=a.pk, actor=owner, source="test:link-audit"):
        events = list(AuditEvent.objects.filter(model_label="sk_tenancy.joinlink").values_list("changes", flat=True))
    assert events and token not in json.dumps(events)
    assert all(event["token_digest"]["redacted"] for event in events if "token_digest" in event)
    assert token not in caplog.text


def test_identity_of_a_link_is_immutable(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    policy(owner, a)
    link, _ = create(owner, a)
    with tenant_context(organization_id=a.pk, actor=owner, source="test:link-immutable"):
        for name, value in (("mode", "approval"), ("max_uses", 99), ("role", role(owner, a, "app_manager"))):
            row = stored_link(owner, a, link)
            setattr(row, name, value)
            with pytest.raises((ImmutableRecord, ValidationError)):
                row.save()


def test_creation_rules_people_manager_and_policy(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    with pytest.raises(ValidationError):
        create(owner, a)  # Links are off without a policy.
    policy(owner, a)
    for kwargs in (
        {"days": 91},
        {"max_uses": 0},
        {"max_uses": 10001},
        {"mode": "auto"},
        {"code": "organization_admin"},
    ):
        with pytest.raises(ValidationError):
            create(owner, a, **kwargs)
    people = member_with_role(owner, a, "link-people@example.test", "people_manager")
    create(owner, a, actor=people)
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        create(owner, a, actor=people, code="app_manager")
    plain = member_with_role(owner, a, "link-plain@example.test", "member")
    assert login(plain).get(reverse("web:join-links", args=[a.pk])).status_code == 403
    assert login(orgs["owner_b"]).get(reverse("web:join-links", args=[a.pk])).status_code == 404


def test_signup_through_link_needs_policy_and_finishes_on_confirmation_elsewhere(orgs):
    from allauth.account.models import EmailAddress, EmailConfirmationHMAC

    from skeleton.accounts.models import User

    owner, a = orgs["owner_a"], orgs["a"]
    policy(owner, a)
    _, token = create(owner, a, mode="direct", max_uses=2)
    anonymous = Client()
    assert anonymous.get(link_url(a, token)).status_code == 200
    closed = anonymous.get(reverse("account_signup"))
    assert b"signup" not in closed.content.lower() or closed.status_code != 200 or "form" not in closed.context
    policy(owner, a, signup=True)
    page = anonymous.get(link_url(a, token))
    assert b"Criar conta" in page.content
    signup = anonymous.post(
        reverse("account_signup"),
        {"email": "link-signup@example.test", "password1": PASSWORD, "password2": PASSWORD},
    )
    assert signup.status_code == 302, signup.content
    user = User.objects.get(email="link-signup@example.test")
    assert active_members(owner, a, user)[0] == []
    with tenant_context(organization_id=a.pk, actor=owner, source="test:link-awaiting"):
        from skeleton.tenancy.models import JoinRequest

        assert JoinRequest.objects.get(user=user).status == "awaiting_verification"
    # Confirmed on another device: no session, no link reference.
    address = EmailAddress.objects.get(user=user, email="link-signup@example.test")
    key = EmailConfirmationHMAC(address).key
    other_device = Client()
    assert other_device.post(reverse("account_confirm_email", args=[key])).status_code == 302
    rows, roles = active_members(owner, a, user)
    assert len(rows) == 1 and roles == ["member"]
    with tenant_context(organization_id=a.pk, actor=owner, source="test:link-approved"):
        request = JoinRequest.objects.get(user=user)
        assert request.status == "approved" and request.decided_by is None
    assert any(user.email in message.to for message in mail.outbox)


def test_link_signup_closed_when_policy_disallows(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    policy(owner, a, signup=False)
    _, token = create(owner, a)
    anonymous = Client()
    assert anonymous.get(link_url(a, token)).status_code == 200
    response = anonymous.post(
        reverse("account_signup"),
        {"email": "link-closed@example.test", "password1": PASSWORD, "password2": PASSWORD},
    )
    from skeleton.accounts.models import User

    assert not User.objects.filter(email="link-closed@example.test").exists()
    assert response.status_code in (200, 302, 403)


def test_api_lists_creates_and_revokes_with_if_match(orgs):
    owner, a = orgs["owner_a"], orgs["a"]
    policy(owner, a)
    client = login(owner)
    body = {"label": "API", "role": str(role(owner, a, "member").pk), "mode": "approval", "days": 3, "max_uses": 2}
    created = client.post(
        reverse("api:organization-join-links", args=[a.pk]), json.dumps(body), content_type="application/json"
    )
    assert created.status_code == 201 and created.json()["url"].count("/join/") == 1
    assert created["Cache-Control"] == "no-store"
    sysid = created.json()["sysid"]
    revoke = reverse("api:organization-join-link-revoke", args=[a.pk, sysid])
    assert client.post(revoke).status_code == 428
    assert client.post(revoke, headers={"If-Match": '"9"'}).status_code == 412
    assert client.post(revoke, headers={"If-Match": '"1"'}).status_code == 200
    assert client.get(reverse("api:organization-join-links", args=[a.pk])).json()["results"] == []
    extra = client.post(
        reverse("api:organization-join-links", args=[a.pk]),
        json.dumps({**body, "use_count": 0}),
        content_type="application/json",
    )
    assert extra.status_code == 400
