"""The platform's configuration of notifications (#784): service, delivery and pages."""

from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.notifications import instance_settings as service
from skeleton.notifications.models import NotificationDelivery, NotificationRecipient
from skeleton.notifications.registry import NotificationField, NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import build_service_scenario
from tests.integration.appearance_support import PASSWORD, acting, login, member, platform_admin

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]

TYPE = "notes.event"
MANDATORY = "notes.alarm"
EXTERNAL = "notes.portal"


@pytest.fixture
def scenario(settings):
    from examples.notes.models import Note

    settings.LANGUAGES = [("pt-br", "Português"), ("en", "English")]
    with isolated_notification_types():
        register_notification_type(
            NotificationType(
                TYPE,
                "Evento",
                payload_fields=("title",),
                fields=(NotificationField("title", "Título"),),
                title="Evento {title}",
                owner_app="example_notes",
            )
        )
        register_notification_type(NotificationType(MANDATORY, "Alarme", mandatory=True, owner_app="example_notes"))
        register_notification_type(
            NotificationType(
                EXTERNAL, "Portal", audience="external", default_channels=("email",), owner_app="example_notes"
            )
        )
        scenario = build_service_scenario(Note, slug="instance-notify")
        scenario.platform = platform_admin("instance-notify-admin@example.test")
        yield scenario


def send(scenario, **payload):
    with scenario.acting(scenario.admin_a, scenario.a):
        notify(TYPE, recipients=Recipients(users=(scenario.member.pk,)), payload={"title": "X", **payload})
    run_pending_jobs()


def rows(scenario):
    with system_context(reason="test:instance-rows", organization_id=scenario.a.pk):
        return list(NotificationRecipient.objects.values_list("title", flat=True))


def deliveries(scenario, channel="email"):
    with system_context(reason="test:instance-deliveries", organization_id=scenario.a.pk):
        return NotificationDelivery.objects.filter(channel=channel).count()


def test_service_rules(scenario, subtests):
    admin = scenario.platform
    with subtests.test("create, then CAS"), acting(admin):
        record = service.set_instance_setting(
            actor=admin, type_code=TYPE, enabled=False, default_channels=["in_app"], expected_revision=None
        )
        with pytest.raises(ConcurrentUpdate):
            service.set_instance_setting(
                actor=admin, type_code=TYPE, enabled=True, default_channels=[], expected_revision=None
            )
        with pytest.raises(ConcurrentUpdate):
            service.set_instance_setting(
                actor=admin, type_code=TYPE, enabled=True, default_channels=[], expected_revision=record.revision + 1
            )
        changed = service.set_instance_setting(
            actor=admin,
            type_code=TYPE,
            enabled=True,
            default_channels=["email", "in_app"],
            expected_revision=record.revision,
        )
        assert changed.default_channels == ["in_app", "email"] and changed.revision == record.revision + 1
    with subtests.test("mandatory, channel and type"), acting(admin):
        with pytest.raises(ValidationError) as caught:
            service.set_instance_setting(
                actor=admin, type_code=MANDATORY, enabled=False, default_channels=[], expected_revision=None
            )
        assert caught.value.error_dict["enabled"][0].code == "mandatory"
        with pytest.raises(ValidationError):
            service.set_instance_setting(
                actor=admin, type_code=EXTERNAL, enabled=True, default_channels=["push"], expected_revision=None
            )
        with pytest.raises(ValidationError):
            service.set_instance_setting(
                actor=admin, type_code="nope.nope", enabled=True, default_channels=[], expected_revision=None
            )
    with subtests.test("texts"), acting(admin):
        with pytest.raises(ValidationError) as caught:
            service.set_instance_text(
                actor=admin, type_code=TYPE, language="pt-br", title="Olá {outro}", body="", expected_revision=None
            )
        assert caught.value.error_dict["title"][0].code == "unknown_placeholder"
        with pytest.raises(ValidationError):
            service.set_instance_text(
                actor=admin, type_code=TYPE, language="fr", title="Olá", body="", expected_revision=None
            )
        text = service.set_instance_text(
            actor=admin, type_code=TYPE, language="pt-br", title="Plataforma: {title}", body="", expected_revision=None
        )
        assert service.instance_texts(TYPE) == {"pt-br": ("Plataforma: {title}", "")}
        service.clear_instance_text(actor=admin, type_code=TYPE, language="pt-br", expected_revision=text.revision)
        assert service.instance_texts(TYPE) == {}
    with subtests.test("only the platform administrator"):
        other = member("instance-notify-member@example.test")
        with acting(other), pytest.raises(PermissionDenied):
            service.set_instance_setting(
                actor=other, type_code=TYPE, enabled=True, default_channels=[], expected_revision=None
            )


def test_turned_off_nobody_receives_and_back_on_they_do(scenario):
    admin = scenario.platform
    with acting(admin):
        record = service.set_instance_setting(
            actor=admin, type_code=TYPE, enabled=False, default_channels=["in_app"], expected_revision=None
        )
    send(scenario)
    assert rows(scenario) == []
    with acting(admin):
        service.set_instance_setting(
            actor=admin, type_code=TYPE, enabled=True, default_channels=["in_app"], expected_revision=record.revision
        )
    send(scenario)
    assert rows(scenario) == ["Evento X"]


def test_default_channels_of_the_instance_come_after_the_person(scenario):
    from skeleton.notifications.preferences import set_preference

    admin = scenario.platform
    with acting(admin):
        service.set_instance_setting(
            actor=admin, type_code=TYPE, enabled=True, default_channels=["in_app", "email"], expected_revision=None
        )
    send(scenario)
    assert deliveries(scenario) == 1  # No preference: the platform's default sends the email.
    with scenario.acting(scenario.member, scenario.a):
        set_preference(scenario.member, type_code=TYPE, channel="email", mode="off", expected_revision=None)
    send(scenario, title="Y")
    assert deliveries(scenario) == 1  # The person's own choice wins.


def test_the_instance_text_is_used_in_its_language(scenario):
    admin = scenario.platform
    with acting(admin):
        service.set_instance_text(
            actor=admin,
            type_code=TYPE,
            language="pt-br",
            title="Da plataforma: {title}",
            body="",
            expected_revision=None,
        )
    send(scenario)
    assert rows(scenario) == ["Da plataforma: X"]


def post(client, url, data=None):
    return client.post(url, {**(data or {}), "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


def test_pages(scenario, subtests):
    from skeleton.accounts.platform import grant_platform_role

    client = login(scenario.platform.email)
    detail = reverse("platform:notification-type", kwargs={"type_code": TYPE})
    with subtests.test("catalog and detail"):
        page = client.get(reverse("platform:notification-types")).content.decode()
        assert "Evento" in page and "Dos aplicativos" in page
        assert "Evento {title}" in client.get(detail).content.decode()
    with subtests.test("channels"):
        response = post(
            client,
            reverse("platform:notification-type-channels", kwargs={"type_code": TYPE}),
            {"channels": ["in_app", "email"], "revision": ""},
        )
        assert response.status_code == 302
        assert service.state_of(TYPE).default_channels == ("in_app", "email")
    with subtests.test("turn off through the confirmation"):
        switch = reverse("platform:notification-type-switch", kwargs={"type_code": TYPE, "action": "disable"})
        assert "Nenhuma organização recebe" in client.get(switch).content.decode()
        revision = service.state_of(TYPE).revision
        assert post(client, switch, {"revision": revision}).status_code == 302
        assert service.state_of(TYPE).enabled is False
        assert post(client, switch, {"revision": revision}).status_code == 409  # Stale revision.
    with subtests.test("text, invalid and valid"):
        url = reverse("platform:notification-type-text", kwargs={"type_code": TYPE, "language": "pt-br"})
        bad = post(client, url, {"title": "Olá {x.__class__}", "body": "", "revision": ""})
        assert bad.status_code == 400
        assert post(client, url, {"title": "Olá {title}", "body": "", "revision": ""}).status_code == 302
    with subtests.test("closed fields and mandatory"):
        url = reverse("platform:notification-type-channels", kwargs={"type_code": TYPE})
        assert post(client, url, {"channels": ["in_app"], "revision": "", "enabled": "1"}).status_code == 400
        switch = reverse("platform:notification-type-switch", kwargs={"type_code": MANDATORY, "action": "disable"})
        assert client.get(switch).status_code == 404
    auditor = member("instance-notify-auditor@example.test")
    with request_context(actor=scenario.platform, request_id=uuid4(), source="test:instance-roles"):
        grant_platform_role(actor=scenario.platform, user=auditor, role="auditor")
    for who, email in (("member", member("instance-notify-other@example.test").email), ("auditor", auditor.email)):
        with subtests.test(who=who):
            other = login(email, PASSWORD)
            assert other.get(reverse("platform:notification-types")).status_code == 403
            assert other.get(detail).status_code == 403
