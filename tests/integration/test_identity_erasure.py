"""The person asks, the platform approves and a job anonymizes the identity (#191)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.accounts.erasure import anonymous_email, decide_erasure, request_erasure
from skeleton.accounts.models import ErasureRequest, User
from skeleton.accounts.services import bootstrap_identity, create_service_account
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests import support

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PLATFORM_PASSWORD = "Erasure-platform-Password!191"


@pytest.fixture
def scenario():
    from examples.notes.models import Note

    with isolated_notification_types():
        register_notification_type(NotificationType("system.notice", "Aviso"))
        yield build_service_scenario(Note, slug="erasure")


@pytest.fixture
def platform():
    user, _ = bootstrap_identity(
        email="erasure-platform@example.test", organization="erasure-platform", name="Plat", password=PLATFORM_PASSWORD
    )
    return user


def login(user, password=FIXTURE_PASSWORD):
    return support.login(user, password=password)


def csrf(client):
    return client.cookies["csrftoken"].value


def ask(client):
    client.get(reverse("web:privacy"))
    return client.post(reverse("web:privacy-erasure"), {"csrfmiddlewaretoken": csrf(client)})


def decide(client, record, decision):
    client.get(reverse("platform:privacy"))
    return client.post(
        reverse("platform:privacy-decide", args=[record.pk]),
        {"decision": decision, "revision": record.revision, "csrfmiddlewaretoken": csrf(client)},
    )


def inbox_owners(organization):
    from skeleton.notifications.models import NotificationRecipient

    with system_context(reason="test:erasure", organization_id=organization.pk):
        return sorted(str(user) for user in NotificationRecipient.objects.values_list("user_id", flat=True))


def personal_token(user):
    from skeleton.integrations.tokens.services import issue_personal_token

    with request_context(actor=user, request_id=uuid4(), source="test:erasure"):
        return issue_personal_token(
            actor=user,
            name="CLI",
            scopes=["example_notes.note:read"],
            expires_at=timezone.now() + timedelta(days=30),
        )


def test_the_request_alone_changes_nothing_and_can_be_withdrawn(scenario):
    client = login(scenario.member)
    assert ask(client).status_code == 302
    record = ErasureRequest.objects.get(user=scenario.member)
    assert record.status == "pending"
    scenario.member.refresh_from_db()
    assert scenario.member.is_active and scenario.member.email == "member@erasure.example.test"
    page = client.get(reverse("web:privacy")).content.decode()
    assert "Retirar o pedido" in page
    assert ask(client).status_code == 302  # A second one is refused (message), not created.
    assert ErasureRequest.objects.filter(user=scenario.member).count() == 1
    response = client.post(
        reverse("web:privacy-erasure"),
        {"intent": "withdraw", "request": record.pk, "revision": record.revision, "csrfmiddlewaretoken": csrf(client)},
    )
    assert response.status_code == 302
    record.refresh_from_db()
    assert record.status == "cancelled"
    assert run_pending_jobs() == []


def test_the_approval_anonymizes_the_identity_and_forgets_its_rows(scenario, platform):
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount

    from skeleton.integrations.tokens.models import ApiToken
    from skeleton.integrations.tokens.services import verify_token
    from skeleton.tenancy.models import Membership

    with scenario.acting(scenario.admin_a, scenario.a):
        notify("system.notice", recipients=Recipients(users=(scenario.member.pk, scenario.denied.pk)))
    with scenario.acting(scenario.admin_b, scenario.b):
        notify("system.notice", recipients=Recipients(users=(scenario.member.pk,)))
    run_pending_jobs()
    issued = personal_token(scenario.member)
    with system_context(reason="test:erasure-social"):
        SocialAccount.objects.create(user=scenario.member, provider="openid_connect", uid="erasure-191")
    with scenario.acting(scenario.member, scenario.a) as member:
        note = scenario.model(organization=scenario.a, code="FICA-1", title="Fica", owner=member)
        note.save()
    session = login(scenario.member)
    assert ask(session).status_code == 302
    record = ErasureRequest.objects.get(user=scenario.member)
    assert decide(login(platform, PLATFORM_PASSWORD), record, "approve").status_code == 302
    record.refresh_from_db()
    assert (record.status, record.decided_by_id) == ("completed", platform.pk)
    assert record.completed_at is not None
    run_pending_jobs()  # The cleanup of every organization.
    from skeleton.audit.models import AuditEvent

    with request_context(actor=platform, request_id=uuid4(), source="test:erasure-audit"):
        trail = list(
            AuditEvent.objects.filter(record_sysid=record.pk)
            .order_by("created_at")
            .values_list("operation", "actor_sysid")
        )
    assert [operation for operation, _ in trail] == ["create", "update"]
    assert [actor for _, actor in trail] == [scenario.member.pk, platform.pk]

    person = User.all_objects.get(pk=scenario.member.pk)
    assert person.email == anonymous_email(person.pk)
    assert (person.first_name, person.last_name, person.is_active) == ("", "", False)
    assert not person.has_usable_password() and not person.local_password_allowed
    assert not EmailAddress.objects.filter(user_id=person.pk).exists()
    assert not SocialAccount.objects.filter(user_id=person.pk).exists()
    assert not ApiToken.objects.filter(user_id=person.pk).exists()
    assert verify_token(issued.text) is None
    # The open session ended (auth_version) and neither email logs in any more.
    assert session.get(reverse("web:privacy")).status_code == 302
    for email in ("member@erasure.example.test", person.email):
        response = Client().post(reverse("account_login"), {"login": email, "password": FIXTURE_PASSWORD})
        assert response.status_code == 200
    # Their inbox rows are gone in A and B; the other person's stay.
    assert inbox_owners(scenario.a) == [str(scenario.denied.pk)]
    assert inbox_owners(scenario.b) == []
    # Business records and memberships stay, pointing at the anonymous identity.
    with system_context(reason="test:erasure", organization_id=scenario.a.pk):
        assert scenario.model.objects.get(pk=note.pk).owner_id == person.pk
        assert Membership.objects.filter(user_id=person.pk).exists()
    # A repeated cleanup finds nothing left; a second decision is refused.
    from skeleton.accounts.erasure import forget_in_organization

    with system_context(reason="test:erasure-repeat", organization_id=scenario.a.pk):
        forget_in_organization(user_id=str(person.pk))
    assert inbox_owners(scenario.a) == [str(scenario.denied.pk)]
    assert decide(login(platform, PLATFORM_PASSWORD), record, "approve").status_code == 302
    record.refresh_from_db()
    assert record.status == "completed"


def test_the_last_administrator_cannot_be_anonymized(scenario, platform):
    ask(login(scenario.admin_b))  # admin-b is the only administrator of B.
    record = ErasureRequest.objects.get(user=scenario.admin_b)
    client = login(platform, PLATFORM_PASSWORD)
    response = decide(client, record, "approve")
    assert response.status_code in {302, 409}, response.status_code
    record.refresh_from_db()
    assert record.status == "pending"
    assert User.all_objects.get(pk=scenario.admin_b.pk).is_active
    assert decide(client, record, "reject").status_code == 302
    record.refresh_from_db()
    assert record.status == "rejected"
    assert run_pending_jobs() == []


def test_service_accounts_and_the_own_request_are_refused(scenario, platform):
    with scenario.acting(scenario.admin_a, scenario.a):
        robot = create_service_account(actor=scenario.admin_a, name="Robô")
    with request_context(actor=robot.pk, request_id=uuid4(), source="test:erasure"):
        with pytest.raises(ValidationError):
            request_erasure(robot)
    with request_context(actor=platform.pk, request_id=uuid4(), source="test:erasure"):
        own = request_erasure(platform)
        with pytest.raises(PermissionDenied):
            decide_erasure(actor=platform, request_id=own.pk, approve=True, expected_revision=own.revision)
    own.refresh_from_db()
    assert own.status == "pending"


def test_only_the_platform_sees_and_decides(scenario, platform):
    ask(login(scenario.member))
    record = ErasureRequest.objects.get(user=scenario.member)
    admin = login(scenario.admin_a)
    assert admin.get(reverse("platform:privacy")).status_code == 403
    response = admin.post(
        reverse("platform:privacy-decide", args=[record.pk]),
        {"decision": "approve", "revision": record.revision, "csrfmiddlewaretoken": csrf(admin)},
    )
    assert response.status_code == 403
    client = login(platform, PLATFORM_PASSWORD)
    page = client.get(reverse("platform:privacy")).content.decode()
    assert "member@erasure.example.test" in page and "Aprovar" in page
    stale = client.post(
        reverse("platform:privacy-decide", args=[record.pk]),
        {"decision": "approve", "revision": record.revision + 1, "csrfmiddlewaretoken": csrf(client)},
    )
    assert stale.status_code == 302
    record.refresh_from_db()
    assert record.status == "pending"  # CAS: a decision on another revision changes nothing.
    assert client.get(reverse("platform:privacy-decide", args=[record.pk])).status_code == 405
