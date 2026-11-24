"""The organization's configuration of notifications (#785): service, delivery, pages, REST and locks."""

import json
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.notifications import organization_settings as service
from skeleton.notifications.models import NotificationDelivery, NotificationRecipient
from skeleton.notifications.registry import NotificationField, NotificationType, register_notification_type
from skeleton.notifications.services import Recipients, notify
from skeleton.service_api.notifications import NotificationService
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_notification_types
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests import support
from tests.integration.appearance_support import acting, platform_admin

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]

APP_TYPE = "notes.event"
ORG_TYPE = "organization.test_notice"
PLATFORM_TYPE = "billing.test_notice"
MANDATORY = "organization.test_alarm"
LOCKED_TEXT = "notes.legal"


@pytest.fixture
def scenario(settings):
    from examples.notes.models import Note

    settings.LANGUAGES = [("pt-br", "Português"), ("en", "English")]
    with isolated_notification_types():
        register_notification_type(
            NotificationType(
                APP_TYPE,
                "Evento",
                payload_fields=("title",),
                fields=(NotificationField("title", "Título"),),
                title="Evento {title}",
                default_channels=("in_app", "email"),
                owner_app="example_notes",
            )
        )
        register_notification_type(
            NotificationType(
                ORG_TYPE, "Aviso da organização", owner_app="sk_tenancy", context="organization", title="Olá"
            )
        )
        register_notification_type(
            NotificationType(PLATFORM_TYPE, "Cobrança", owner_app="sk_billing", context="platform", title="Fatura")
        )
        register_notification_type(
            NotificationType(MANDATORY, "Alarme", owner_app="sk_tenancy", context="organization", mandatory=True)
        )
        register_notification_type(
            NotificationType(LOCKED_TEXT, "Termo", owner_app="example_notes", org_editable=False, title="Texto legal")
        )
        yield build_service_scenario(Note, slug="org-notify")


def send(scenario, organization=None, kind=APP_TYPE):
    organization = organization or scenario.a
    admin = scenario.admin_a if organization.pk == scenario.a.pk else scenario.admin_b
    with scenario.acting(admin, organization):
        notify(
            kind, recipients=Recipients(users=(scenario.member.pk,)), payload={"title": "X"} if kind == APP_TYPE else {}
        )
    run_pending_jobs()


def titles(organization):
    with system_context(reason="test:org-notify-rows", organization_id=organization.pk):
        return sorted(NotificationRecipient.objects.values_list("title", flat=True))


def test_service_rules(scenario, subtests):
    with subtests.test("default deny"), scenario.acting(scenario.member):
        with pytest.raises(PermissionDenied):
            service.organization_overview(actor=scenario.member)
        with pytest.raises(PermissionDenied):
            service.set_enabled(actor=scenario.member, type_code=APP_TYPE, enabled=False, expected_revision=None)
    with subtests.test("overview"), scenario.acting(scenario.admin_a):
        codes = {config.kind.code for config in service.organization_overview(actor=scenario.admin_a)}
        assert {APP_TYPE, ORG_TYPE, PLATFORM_TYPE, MANDATORY} <= codes
    with subtests.test("refusals"), scenario.acting(scenario.admin_a):
        for code, expected in ((PLATFORM_TYPE, "platform_type"), (MANDATORY, "mandatory")):
            with pytest.raises(ValidationError) as caught:
                service.set_enabled(actor=scenario.admin_a, type_code=code, enabled=False, expected_revision=None)
            assert caught.value.error_dict["enabled"][0].code == expected
        for code, expected in ((PLATFORM_TYPE, "platform_type"), (LOCKED_TEXT, "not_editable")):
            with pytest.raises(ValidationError) as caught:
                service.set_text(
                    actor=scenario.admin_a, type_code=code, language="pt-br", title="X", body="", expected_revision=None
                )
            assert caught.value.error_dict["title"][0].code == expected
        with pytest.raises(service.UnknownType):
            service.set_enabled(actor=scenario.admin_a, type_code="nope.nope", enabled=False, expected_revision=None)
    with subtests.test("CAS"), scenario.acting(scenario.admin_a):
        record = service.set_enabled(actor=scenario.admin_a, type_code=APP_TYPE, enabled=False, expected_revision=None)
        with pytest.raises(ConcurrentUpdate):
            service.set_enabled(actor=scenario.admin_a, type_code=APP_TYPE, enabled=True, expected_revision=None)
        with pytest.raises(ConcurrentUpdate):
            service.set_enabled(
                actor=scenario.admin_a, type_code=APP_TYPE, enabled=True, expected_revision=record.revision + 1
            )


def test_turned_off_in_a_nobody_there_receives_b_still_does(scenario):
    with scenario.acting(scenario.admin_a):
        service.set_enabled(actor=scenario.admin_a, type_code=APP_TYPE, enabled=False, expected_revision=None)
    send(scenario, scenario.a)
    send(scenario, scenario.b)
    assert titles(scenario.a) == []
    assert titles(scenario.b) == ["Evento X"]
    with system_context(reason="test:org-notify-email", organization_id=scenario.a.pk):
        assert not NotificationDelivery.objects.exists()


def test_the_organization_text_wins_only_in_its_organization(scenario):
    from skeleton.notifications import instance_settings

    admin = platform_admin("org-notify-platform@example.test")
    with acting(admin):
        instance_settings.set_instance_text(
            actor=admin,
            type_code=APP_TYPE,
            language="pt-br",
            title="Plataforma {title}",
            body="",
            expected_revision=None,
        )
    with scenario.acting(scenario.admin_a):
        service.set_text(
            actor=scenario.admin_a,
            type_code=APP_TYPE,
            language="pt-br",
            title="Org A {title}",
            body="",
            expected_revision=None,
        )
    send(scenario, scenario.a)
    send(scenario, scenario.b)
    assert titles(scenario.a) == ["Org A X"]
    assert titles(scenario.b) == ["Plataforma X"]
    with scenario.acting(scenario.admin_b, scenario.b), pytest.raises(service.UnknownType):
        service.kind_for_organization("nope.nope")
    with scenario.acting(scenario.admin_b, scenario.b):
        assert service.organization_texts(APP_TYPE) == {}  # A's text is never visible from B.


def test_a_platform_lock_refuses_the_organization(scenario):
    from skeleton.tenancy.setting_locks import lock_setting

    admin = platform_admin("org-notify-locker@example.test")
    with request_context(actor=admin, request_id=uuid4(), source="test:org-notify-lock"):
        lock_setting(
            actor=admin,
            organization=scenario.a,
            key=f"notifications.{APP_TYPE}.enabled",
            value=False,
            reason="Contrato",
        )
    send(scenario, scenario.a)
    assert titles(scenario.a) == []  # The lock wrote the value.
    with scenario.acting(scenario.admin_a), pytest.raises(ValidationError) as caught:
        service.set_enabled(actor=scenario.admin_a, type_code=APP_TYPE, enabled=True, expected_revision=None)
    assert caught.value.error_dict["enabled"][0].code == "setting_locked"


def test_the_app_type_disappears_with_the_app(scenario):
    from skeleton.testing.service_api import _set_enabled

    with scenario.acting(scenario.admin_a):
        _set_enabled("example_notes", scenario.admin_a, enabled=False)
        codes = {config.kind.code for config in service.organization_overview(actor=scenario.admin_a)}
    assert APP_TYPE not in codes and ORG_TYPE in codes


def test_the_app_reads_its_own_types(scenario):
    namespace = {"__name__": "examples.notes.services", "function": NotificationService().settings}
    exec("def call(*args, **kwargs):\n    return function(*args, **kwargs)\n", namespace)  # noqa: S102
    with scenario.acting(scenario.admin_a):
        service.set_enabled(actor=scenario.admin_a, type_code=APP_TYPE, enabled=False, expected_revision=None)
        result = namespace["call"](actor=scenario.admin_a)
    assert {item.type_code: (item.enabled, item.reason) for item in result} == {
        APP_TYPE: (False, "organization_disabled"),
        LOCKED_TEXT: (True, ""),
    }


def post(client, url, data=None):
    return client.post(url, {**(data or {}), "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


def test_pages_and_api(scenario, subtests):
    org = scenario.a.pk
    admin = support.login(scenario.admin_a, password=FIXTURE_PASSWORD)
    listing = reverse("notifications:organization-settings", kwargs={"organization_id": org})
    detail = reverse("notifications:organization-setting", kwargs={"organization_id": org, "type_code": APP_TYPE})
    with subtests.test("list and detail"):
        page = admin.get(listing).content.decode()
        assert "Evento" in page and "Da plataforma" in page and f"<code>{APP_TYPE}</code>" not in page
        assert admin.get(detail).status_code == 200
        assert (
            "Notificações"
            in admin.get(reverse("web:organization-admin", kwargs={"organization_id": org})).content.decode()
        )
    with subtests.test("turn off through the confirmation"):
        switch = reverse(
            "notifications:organization-setting-switch",
            kwargs={"organization_id": org, "type_code": APP_TYPE, "action": "disable"},
        )
        assert "Nenhum membro da organização" in admin.get(switch).content.decode()
        assert post(admin, switch, {"revision": ""}).status_code == 302
        assert post(admin, switch, {"revision": ""}).status_code == 409  # Stale: the row exists now.
    with subtests.test("text"):
        url = reverse(
            "notifications:organization-setting-text",
            kwargs={"organization_id": org, "type_code": APP_TYPE, "language": "pt-br"},
        )
        assert post(admin, url, {"title": "{x.y}", "body": "", "revision": ""}).status_code == 400
        assert (
            post(admin, url, {"title": "Nosso {title}", "body": "", "revision": "", "organization": "x"}).status_code
            == 400
        )
        assert post(admin, url, {"title": "Nosso {title}", "body": "", "revision": ""}).status_code == 302
    with subtests.test("markers of the recipient and of general use (#806)"):
        fixed = "Olá, {destinatario_primeiro_nome}: {title} em {plataforma}, {data_atual}"
        assert post(admin, url, {"title": fixed, "body": "{destinatario}", "revision": "1"}).status_code == 302
        page = admin.get(detail).content.decode()
        assert "<code>{destinatario}</code>" in page and "<code>{data_atual}</code>" in page
        assert "Olá, Maria: " in page and "Maria Exemplo" in page  # The preview fills them with samples.
    with subtests.test("platform type is read only"):
        platform_detail = reverse(
            "notifications:organization-setting", kwargs={"organization_id": org, "type_code": PLATFORM_TYPE}
        )
        page = admin.get(platform_detail).content.decode()
        assert "definido pela plataforma" in page.lower() and 'name="title"' not in page
        switch = reverse(
            "notifications:organization-setting-switch",
            kwargs={"organization_id": org, "type_code": PLATFORM_TYPE, "action": "disable"},
        )
        assert admin.get(switch).status_code == 404
    with subtests.test("member without the capability and another organization"):
        member = support.login(scenario.member, password=FIXTURE_PASSWORD)
        assert member.get(listing).status_code == 403  # tenant_page refuses without the capability.
        assert member.get(detail).status_code == 403
        other = support.login(scenario.admin_b, password=FIXTURE_PASSWORD)
        assert other.get(listing).status_code == 404  # Not a member of A: answered like a missing page.
    with subtests.test("REST"):
        api = reverse("notifications:api-organization-setting", kwargs={"organization_id": org, "type_code": ORG_TYPE})
        listing_api = admin.get(reverse("notifications:api-organization-settings", kwargs={"organization_id": org}))
        assert ORG_TYPE in {item["type_code"] for item in listing_api.json()["results"]}
        token = admin.cookies["csrftoken"].value
        created = admin.put(
            api, json.dumps({"enabled": False}), content_type="application/json", HTTP_X_CSRFTOKEN=token
        )
        assert created.status_code == 200 and created.json()["enabled"] is False
        again = admin.put(api, json.dumps({"enabled": True}), content_type="application/json", HTTP_X_CSRFTOKEN=token)
        assert again.status_code == 428
        stale = admin.put(
            api,
            json.dumps({"enabled": True}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
            HTTP_IF_MATCH='"99"',
        )
        assert stale.status_code == 412
        revision = created.json()["revision"]
        ok = admin.put(
            api,
            json.dumps({"enabled": True}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
            HTTP_IF_MATCH=f'"{revision}"',
        )
        assert ok.status_code == 200 and ok.json()["enabled"] is True
        assert member.get(api).status_code == 404
