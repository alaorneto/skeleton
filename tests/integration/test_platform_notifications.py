"""The platform's own inbox (#787): notify_platform, fan-out, email and the pages."""

from uuid import uuid4

import pytest
from django.core import mail
from django.urls import reverse

from skeleton.notifications.models import PlatformNotificationRecipient
from skeleton.notifications.platform import NotAPlatformType, notify_platform, unread_count
from skeleton.notifications.registry import NotificationField, NotificationType, register_notification_type
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from tests.integration.appearance_support import PASSWORD, acting, login, member, platform_admin

pytestmark = pytest.mark.usefixtures("skeleton_db")

ADMINS = "platform.test_admins"
OPERATORS = "platform.test_operators"


@pytest.fixture
def people(settings):
    settings.SKELETON_PUBLIC_URL = "https://app.example.test"
    with isolated_notification_types():
        common = {"owner_app": "sk_jobs", "context": "platform", "audience": "platform"}
        register_notification_type(
            NotificationType(
                ADMINS,
                "Para administradores",
                payload_fields=("count",),
                fields=(NotificationField("count", "Quantidade", kind="number"),),
                title="Há {count} jobs que falharam",
                default_channels=("in_app", "email"),
                open_url=lambda user, row: reverse("platform:jobs"),
                **common,
            )
        )
        register_notification_type(
            NotificationType(OPERATORS, "Para operadores", platform_capability="updates.operate", **common)
        )
        register_notification_type(NotificationType("tasks.other", "Outro", owner_app="tasks"))
        admin = platform_admin("platform-inbox-admin@example.test")
        operator = member("platform-inbox-operator@example.test")
        plain = member("platform-inbox-plain@example.test")
        from skeleton.accounts.platform import grant_platform_role

        with request_context(actor=admin, request_id=uuid4(), source="test:platform-inbox"):
            grant_platform_role(actor=admin, user=operator, role="operator")
        yield admin, operator, plain


def rows(user):
    with system_context(reason="test:platform-inbox-rows"):
        return list(PlatformNotificationRecipient.objects.filter(user_id=user.pk).values_list("title", "in_app"))


def send(type_code, **kwargs):
    with system_context(reason="test:platform-notify"):
        notification = notify_platform(type_code, **kwargs)
    run_pending_jobs()
    return notification


def test_recipients_are_the_administrators_and_the_holders_of_the_capability(people):
    admin, operator, plain = people
    mail.outbox.clear()
    send(ADMINS, payload={"count": 1234})
    send(OPERATORS)
    assert sorted(rows(admin)) == [("Há 1.234 jobs que falharam", True), ("Para operadores", True)]
    assert rows(operator) == [("Para operadores", True)]
    assert rows(plain) == []
    emails = [item for item in mail.outbox if admin.email in item.to]
    assert len(emails) == 1 and emails[0].subject == "Há 1.234 jobs que falharam"
    assert "https://app.example.test/platform/notifications/" in emails[0].body


def test_notify_platform_refusals_and_dedupe(people):
    first = send(ADMINS, payload={"count": 1}, dedupe_key="jobs:1")
    again = send(ADMINS, payload={"count": 1}, dedupe_key="jobs:1")
    assert first.pk == again.pk
    with system_context(reason="test:platform-refuse"), pytest.raises(NotAPlatformType):
        notify_platform("tasks.other")


def test_inside_a_tenant_it_is_refused(people):
    from skeleton.testing.people import make_organization

    admin, _operator, _plain = people
    organization = make_organization(owner=admin, slug="platform-inbox-org", name="Org")
    with system_context(reason="test:platform-tenant", organization_id=organization.pk), pytest.raises(RuntimeError):
        notify_platform(ADMINS, payload={"count": 1})


def test_turned_off_by_the_platform_nobody_receives(people):
    from skeleton.notifications import instance_settings

    admin, _operator, _plain = people
    with acting(admin):
        instance_settings.set_instance_setting(
            actor=admin, type_code=ADMINS, enabled=False, default_channels=["in_app"], expected_revision=None
        )
    send(ADMINS, payload={"count": 1})
    assert rows(admin) == []


def test_losing_the_role_closes_the_inbox_and_skips_the_email(people):
    from skeleton.accounts.models import PlatformRoleAssignment
    from skeleton.accounts.platform import revoke_platform_role
    from skeleton.notifications import instance_settings

    admin, operator, _plain = people
    with acting(admin):
        instance_settings.set_instance_setting(
            actor=admin, type_code=OPERATORS, enabled=True, default_channels=["in_app", "email"], expected_revision=None
        )
    with system_context(reason="test:platform-email-skip"):
        notify_platform(OPERATORS)
    assignment = PlatformRoleAssignment.objects.get(user=operator)
    with request_context(actor=admin, request_id=uuid4(), source="test:platform-revoke"):
        revoke_platform_role(actor=admin, assignment_id=assignment.pk, expected_revision=assignment.revision)
    mail.outbox.clear()
    run_pending_jobs()  # The fan-out runs now: the operator lost the role before it.
    assert rows(operator) == [] and not [item for item in mail.outbox if operator.email in item.to]
    client = login(operator.email, PASSWORD)
    assert client.get(reverse("platform:notifications")).status_code == 403  # Revocation is immediate.


def test_retention_removes_old_read_rows(people):
    from datetime import timedelta

    from django.utils import timezone

    from skeleton.notifications.models import PlatformNotification
    from skeleton.notifications.platform import purge_platform_notifications

    admin, _operator, _plain = people
    send(ADMINS, payload={"count": 1})
    with system_context(reason="test:platform-retention"):
        PlatformNotificationRecipient.objects.filter(user=admin).update(read_at=timezone.now())
        assert purge_platform_notifications() == {"rows": 0, "notifications": 0}  # Inside the deadline.
        later = timezone.now() + timedelta(days=400)
        assert purge_platform_notifications(now=later) == {"rows": 1, "notifications": 1}
        assert not PlatformNotificationRecipient.objects.exists()
        assert not PlatformNotification.objects.exists()


def post(client, url, data=None):
    return client.post(url, {**(data or {}), "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


def test_inbox_pages(people, subtests):
    admin, operator, plain = people
    send(ADMINS, payload={"count": 3})
    send(OPERATORS)
    with system_context(reason="test:platform-inbox-pages"):
        admin_row = PlatformNotificationRecipient.objects.get(user=admin, type_code=ADMINS)
        operator_row = PlatformNotificationRecipient.objects.get(user=operator)
    client = login(admin.email)
    with subtests.test("list and counter"):
        page = client.get(reverse("platform:notifications")).content.decode()
        assert "Há 3 jobs que falharam" in page and "Para operadores" in page
        assert unread_count(admin) == 2
    with subtests.test("open leads to the type's page and marks read"):
        response = client.get(reverse("platform:notification-open", kwargs={"sysid": admin_row.pk}))
        assert response.status_code == 302 and response["Location"] == reverse("platform:jobs")
        assert unread_count(admin) == 1
    with subtests.test("actions"):
        change = reverse("platform:notification-change", kwargs={"sysid": admin_row.pk, "action": "unread"})
        assert post(client, change).status_code == 302 and unread_count(admin) == 2
        assert post(client, reverse("platform:notification-read-all")).status_code == 302
        assert unread_count(admin) == 0
    with subtests.test("another person's row answers 404"):
        assert client.get(reverse("platform:notification-open", kwargs={"sysid": operator_row.pk})).status_code == 404
        other = reverse("platform:notification-change", kwargs={"sysid": operator_row.pk, "action": "archive"})
        assert post(client, other).status_code == 404
    with subtests.test("a limited role sees its own inbox, not the catalog"):
        operator_client = login(operator.email, PASSWORD)
        assert "Para operadores" in operator_client.get(reverse("platform:notifications")).content.decode()
        assert operator_client.get(reverse("platform:notification-types")).status_code == 403
    with subtests.test("a person without a platform role is refused"):
        assert login(plain.email, PASSWORD).get(reverse("platform:notifications")).status_code == 403
