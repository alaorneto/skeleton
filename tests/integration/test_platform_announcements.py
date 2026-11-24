"""Announcements of the platform to the organizations (#789)."""

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.urls import reverse

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.notifications import announcements as service
from skeleton.notifications.models import NotificationRecipient
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.people import add_member, make_organization, make_person
from tests.integration.appearance_support import acting, login, member, platform_admin

pytestmark = pytest.mark.usefixtures("skeleton_db")


@pytest.fixture
def world():
    admin = platform_admin("announce-admin@example.test")
    owner_a = make_person(email="announce-owner-a@example.test")
    owner_b = make_person(email="announce-owner-b@example.test")
    a = make_organization(owner=owner_a, slug="announce-a", name="A")
    b = make_organization(owner=owner_b, slug="announce-b", name="B")
    plain = make_person(email="announce-plain@example.test")
    add_member(organization=a, user=plain)
    gone = make_organization(owner=make_person(email="announce-gone@example.test"), slug="announce-gone", name="X")
    with system_context(reason="test:announce-deactivate"):
        gone.is_active = False
        gone.save(update_fields={"is_active"})
    run_pending_jobs()
    return admin, a, b, owner_a, owner_b, plain


def announcement_rows(organization):
    with system_context(reason="test:announce-rows", organization_id=organization.pk):
        return sorted(
            NotificationRecipient.objects.filter(type_code=service.TYPE).values_list("user_id", "title", "body")
        )


def create(admin, **kwargs):
    values = {
        "title": "Manutenção {sábado}",
        "body": "Das 2h às 4h.\nSem acesso.",
        "audience": "admins",
        "organizations": [],
    }
    values.update(kwargs)
    with acting(admin):
        return service.create_announcement(actor=admin, **values)


def send(admin, record):
    with acting(admin):
        sent = service.send_announcement(actor=admin, announcement_id=record.pk, expected_revision=record.revision)
    run_pending_jobs()
    return sent


def test_admins_of_every_active_organization_receive_it_once(world):
    admin, a, b, owner_a, owner_b, plain = world
    record = create(admin)
    sent = send(admin, record)
    from skeleton.tenancy.models import Organization

    active = Organization.objects.filter(is_active=True).count()
    assert not Organization.objects.get(slug="announce-gone").is_active
    assert sent.state == "sent" and sent.organization_count == active  # The inactive organization is left out.
    body = "Das 2h às 4h.\nSem acesso."
    assert announcement_rows(a) == [(owner_a.pk, "Manutenção {sábado}", body)]  # Braces are just text.
    assert announcement_rows(b) == [(owner_b.pk, "Manutenção {sábado}", body)]
    run_pending_jobs()
    assert len(announcement_rows(a)) == 1  # Running the jobs again sends nothing twice.


def test_every_member_of_the_chosen_organizations(world):
    admin, a, b, owner_a, _owner_b, plain = world
    record = create(admin, audience="members", organizations=["announce-a"])
    send(admin, record)
    assert {row[0] for row in announcement_rows(a)} == {owner_a.pk, plain.pk}
    assert announcement_rows(b) == []


def test_drafts_change_with_cas_and_sent_never_changes(world):
    admin, *_ = world
    record = create(admin)
    with acting(admin):
        changed = service.update_announcement(
            actor=admin,
            announcement_id=record.pk,
            expected_revision=record.revision,
            title="Novo",
            body="",
            audience="members",
            organizations=[],
        )
        with pytest.raises(ConcurrentUpdate):
            service.update_announcement(
                actor=admin,
                announcement_id=record.pk,
                expected_revision=record.revision,
                title="Velho",
                body="",
                audience="admins",
                organizations=[],
            )
    sent = send(admin, changed)
    with acting(admin), pytest.raises(ValidationError):
        service.update_announcement(
            actor=admin,
            announcement_id=sent.pk,
            expected_revision=sent.revision,
            title="Depois",
            body="",
            audience="admins",
            organizations=[],
        )
    with acting(admin), pytest.raises(ValidationError):
        service.delete_announcement(actor=admin, announcement_id=sent.pk, expected_revision=sent.revision)


def test_refusals(world):
    admin, *_ = world
    with pytest.raises(ValidationError) as caught:
        create(admin, organizations=["announce-a", "nao-existe", "announce-gone"])
    assert "nao-existe" in str(caught.value) and "announce-gone" in str(caught.value)
    with pytest.raises(ValidationError):
        create(admin, title="")
    with pytest.raises(ValidationError):
        create(admin, audience="everyone")
    other = member("announce-member@example.test")
    with acting(other), pytest.raises(PermissionDenied):
        service.create_announcement(actor=other, title="X", body="", audience="admins", organizations=[])


def test_the_organization_cannot_turn_it_off_nor_rewrite_it(world):
    from skeleton.notifications import instance_settings, organization_settings
    from skeleton.testing.service_api import acting_as

    admin, a, _b, owner_a, *_ = world
    with acting_as(owner_a, a), pytest.raises(ValidationError):
        organization_settings.set_enabled(actor=owner_a, type_code=service.TYPE, enabled=False, expected_revision=None)
    with acting(admin), pytest.raises(ValidationError) as caught:
        instance_settings.set_instance_text(
            actor=admin, type_code=service.TYPE, language="pt-br", title="X", body="", expected_revision=None
        )
    assert caught.value.error_dict["title"][0].code == "not_editable"


def post(client, url, data=None):
    return client.post(url, {**(data or {}), "csrfmiddlewaretoken": client.cookies["csrftoken"].value})


def test_pages(world, subtests):
    admin, a, *_ = world
    client = login(admin.email)
    with subtests.test("new draft and review"):
        response = post(
            client,
            reverse("platform:announcement-create"),
            {
                "title": "Aviso",
                "body": "Texto",
                "audience": "admins",
                "scope": "list",
                "organizations": "announce-a",
                "action": "review",
            },
        )
        assert response.status_code == 302 and response["Location"].endswith("/send/")
    from skeleton.notifications.models import PlatformAnnouncement

    record = PlatformAnnouncement.objects.get(title="Aviso")
    send_url = reverse("platform:announcement-send", kwargs={"sysid": record.pk})
    with subtests.test("confirmation names the count"):
        assert "Enviar a 1 organizações?" in client.get(send_url).content.decode()
    with subtests.test("send, then the page is read only"):
        assert post(client, send_url, {"revision": record.revision}).status_code == 302
        detail = reverse("platform:announcement", kwargs={"sysid": record.pk})
        assert "não pode ser alterado" in client.get(detail).content.decode()
        assert post(client, detail, {"title": "X", "revision": record.revision}).status_code == 400
        assert client.get(send_url).status_code == 404
    with subtests.test("invalid organization keeps the form"):
        response = post(
            client,
            reverse("platform:announcement-create"),
            {
                "title": "Outro",
                "body": "",
                "audience": "admins",
                "scope": "list",
                "organizations": "nada",
                "action": "save",
            },
        )
        assert response.status_code == 400 and "nada" in response.content.decode()
    with subtests.test("a limited role or a member is refused"):
        other = login(member("announce-other@example.test").email)
        assert other.get(reverse("platform:announcements")).status_code == 403
