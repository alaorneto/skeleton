"""Erasure of a person's identity (#191): requested by the person, approved by the platform."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.accounts.erasure import anonymize_organization, anonymized_email
from skeleton.accounts.models import ErasureRequest, User
from skeleton.accounts.services import bootstrap_identity
from skeleton.notifications import registry
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")
PLATFORM_PASSWORD = "Erasure-platform-Password!191"


@pytest.fixture
def scenario(monkeypatch):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    monkeypatch.setattr(registry, "_types", {})
    monkeypatch.setattr(registry, "_duplicates", [])
    register_notification_type(NotificationType("system.notice", "Aviso"))
    return build_service_scenario(Note, slug="erasure")


@pytest.fixture
def platform_admin():
    user, _ = bootstrap_identity(
        email="erasure-platform@example.test", organization="erasure-platform", name="P", password=PLATFORM_PASSWORD
    )
    return user


def login(email, password=FIXTURE_PASSWORD):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": email, "password": password, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302, response.status_code
    return client


def post(client, url, data=None):
    return client.post(url, {**(data or {}), "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


def ask(client):
    response = post(client, reverse("web:privacy-erasure"), {"confirm": "on"})
    assert response.status_code == 302, response.status_code


def latest(user):
    with system_context(reason="test:erasure-read"):
        return ErasureRequest.objects.filter(user_id=user.pk).order_by("-created_at").first()


def decide(client, row, action, **extra):
    return post(
        client,
        reverse("platform:privacy-decision", args=[row.pk]),
        {"action": action, "expected_revision": row.revision, **extra},
    )


def fresh(user):
    return User.all_objects.get(pk=user.pk)


def notification_rows(organization, user):
    from skeleton.notifications.models import NotificationRecipient

    with system_context(reason="test:erasure-rows", organization_id=organization.pk):
        return NotificationRecipient.objects.filter(user_id=user.pk).count()


def membership_active(organization, user):
    from skeleton.tenancy.models import Membership

    with system_context(reason="test:erasure-membership", organization_id=organization.pk):
        return Membership.objects.get(user_id=user.pk).is_active


def personal_token(user):
    from skeleton.integrations.tokens.services import issue_personal_token

    with request_context(actor=user, request_id=uuid4(), source="test:erasure-token"):
        return issue_personal_token(
            actor=user,
            name="CLI",
            scopes=["example_notes.note:read"],
            expires_at=timezone.now() + timedelta(days=30),
        )


def test_nothing_changes_before_approval_then_the_identity_is_anonymized(scenario, platform_admin):
    from allauth.account.models import EmailAddress

    from skeleton.integrations.tokens.services import verify_token

    person = scenario.member
    note = scenario.create(scenario.a, owner=person, code="E-1", title="Registro da organização")
    token = personal_token(person)
    for admin, organization in ((scenario.admin_a, scenario.a), (scenario.admin_b, scenario.b)):
        with scenario.acting(admin, organization):
            notify("system.notice", recipients=Recipients(users=(person.pk,)))
    run_pending_jobs()
    assert notification_rows(scenario.a, person) == notification_rows(scenario.b, person) == 1
    session = login(person.email)
    ask(session)
    ask(session)  # A second request returns the open one.
    with system_context(reason="test:erasure-count"):
        assert ErasureRequest.objects.filter(user_id=person.pk).count() == 1
    row = latest(person)
    assert row.state == "pending"
    page = session.get(reverse("web:privacy")).content.decode()
    assert "Cancelar o pedido" in page
    run_pending_jobs()
    assert fresh(person).email == person.email and fresh(person).is_active  # Not approved: unchanged.

    operator = login(platform_admin.email, PLATFORM_PASSWORD)
    listing = operator.get(reverse("platform:privacy")).content.decode()
    assert person.email in listing and reverse("platform:privacy-decision", args=[row.pk]) in listing
    assert decide(operator, row, "approve").status_code == 302
    assert latest(person).state == "completed"  # The identity is anonymized in the approval.
    assert session.get(reverse("web:privacy")).status_code == 302  # The old session ended at once.
    run_pending_jobs()  # The cleanup of each organization.

    user = fresh(person)
    assert user.email == anonymized_email(user) and (user.first_name, user.last_name) == ("", "")
    assert not user.is_active and not user.has_usable_password() and not user.local_password_allowed
    assert latest(person).state == "completed" and latest(person).completed_at is not None
    with system_context(reason="test:erasure-copies"):
        assert not EmailAddress.objects.filter(user_id=person.pk).exists()
        assert fresh(person).profile.display_name == ""
    assert verify_token(token.text) is None
    anonymous = Client()
    assert (
        anonymous.post(reverse("account_login"), {"login": person.email, "password": FIXTURE_PASSWORD}).status_code
        == 200
    )
    for organization in (scenario.a, scenario.b):
        assert notification_rows(organization, person) == 0
        assert membership_active(organization, person) is False
    with system_context(reason="test:erasure-note", organization_id=scenario.a.pk):
        kept = scenario.model.objects.get(pk=note.pk)
        assert kept.owner_id == person.pk and kept.title == "Registro da organização"
    # A/B: the other people of both organizations are untouched.
    for other in (scenario.admin_a, scenario.admin_b, scenario.denied):
        assert fresh(other).is_active and fresh(other).email == other.email

    # Deciding again or running the cleanup again changes nothing.
    completed = latest(person)
    decide(operator, completed, "approve")
    assert latest(person).revision == completed.revision and latest(person).state == "completed"
    with system_context(reason="test:erasure-rerun-org", organization_id=scenario.a.pk):
        assert anonymize_organization(user=person.pk) == 0
    assert fresh(person).auth_version == user.auth_version


def test_the_last_administrator_of_an_organization_is_blocked(scenario, platform_admin):
    ask(login(scenario.admin_a.email))
    row = latest(scenario.admin_a)
    operator = login(platform_admin.email, PLATFORM_PASSWORD)
    response = decide(operator, row, "approve")
    assert response.status_code == 302
    messages = operator.get(reverse("platform:privacy")).content.decode()
    assert "última administradora" in messages
    assert latest(scenario.admin_a).state == "pending"
    run_pending_jobs()
    assert fresh(scenario.admin_a).is_active and fresh(scenario.admin_a).email == scenario.admin_a.email


def test_only_the_person_cancels_and_only_another_platform_administrator_decides(scenario, platform_admin):
    ask(login(scenario.member.email))
    row = latest(scenario.member)
    # Another person cannot cancel it: the request is not theirs.
    other = login(scenario.denied.email)
    post(other, reverse("web:privacy-erasure-cancel"), {"request": row.pk, "expected_revision": row.revision})
    assert latest(scenario.member).state == "pending"
    # A member of an organization is not a platform administrator.
    assert decide(other, row, "approve").status_code == 403
    assert latest(scenario.member).state == "pending"
    # A platform administrator never decides their own request.
    operator = login(platform_admin.email, PLATFORM_PASSWORD)
    ask(operator)
    own = latest(platform_admin)
    decide(operator, own, "approve")
    assert latest(platform_admin).state == "pending"
    # Stale revision: nothing happens.
    stale = decide(operator, row, "reject", note="x", expected_revision=row.revision + 1)
    assert stale.status_code == 302 and latest(scenario.member).state == "pending"
    # The person cancels it.
    mine = login(scenario.member.email)
    post(mine, reverse("web:privacy-erasure-cancel"), {"request": row.pk, "expected_revision": row.revision})
    assert latest(scenario.member).state == "cancelled"


def test_a_rejection_needs_a_note_the_person_reads(scenario, platform_admin):
    session = login(scenario.member.email)
    ask(session)
    row = latest(scenario.member)
    operator = login(platform_admin.email, PLATFORM_PASSWORD)
    decide(operator, row, "reject")
    assert latest(scenario.member).state == "pending"  # No note.
    decide(operator, row, "reject", note="Há uma obrigação legal de guarda em andamento.")
    rejected = latest(scenario.member)
    assert rejected.state == "rejected" and rejected.decided_by == platform_admin.pk
    assert "obrigação legal" in session.get(reverse("web:privacy")).content.decode()
    assert fresh(scenario.member).is_active
    ask(session)  # A new request may follow a rejection.
    assert latest(scenario.member).state == "pending"


def test_the_request_and_the_decision_are_audited(scenario, platform_admin):
    import psycopg

    from tools.integration_database import disposable_database_pair

    ask(login(scenario.member.email))
    row = latest(scenario.member)
    decide(login(platform_admin.email, PLATFORM_PASSWORD), row, "approve")
    with psycopg.connect(disposable_database_pair().owner) as connection:
        operations = connection.execute(
            "SELECT operation, actor_sysid FROM skeleton.sk_audit_auditevent "
            "WHERE table_name = 'sk_accounts_erasurerequest' AND record_sysid = %s ORDER BY record_revision",
            [row.pk],
        ).fetchall()
    assert [operation for operation, _ in operations] == ["create", "update"]
    assert [actor for _, actor in operations] == [scenario.member.pk, platform_admin.pk]
