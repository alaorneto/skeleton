"""Calendars page, sharing and managed calendars (#670)."""

import re
from uuid import uuid4

import pytest
from django.urls import reverse

from skeleton.contrib.calendar.models import Calendar, valid_source_path
from skeleton.contrib.calendar.service_api import CalendarService
from skeleton.service_api import Conflict, Forbidden, InvalidInput, NotFound
from skeleton.service_api.access import AccessService
from skeleton.tenancy.context import system_context
from skeleton.testing.applications import activate_app, open_app
from tools.security_fixtures import actor_tenant

SOURCE = "example_notes"
PATH = f"apps/{SOURCE}/schedules/3/"
TABLE = "skeleton_calendar.calendar"


def url(agenda, name, row=None, organization=None):
    kwargs = {"organization_id": (organization or agenda.a).pk}
    if row is not None:
        kwargs["sysid"] = getattr(row, "pk", getattr(row, "sysid", row))
    return reverse(f"skeleton_calendar:{name}", kwargs=kwargs)


def post(client, address, data=None):
    return client.post(address, {"csrfmiddlewaretoken": client.cookies["csrftoken"].value, **(data or {})})


def shared(agenda, *, source_app="", source_path=""):
    with actor_tenant(agenda.admin, agenda.a):
        return CalendarService().create_shared_calendar(
            actor=agenda.admin,
            name="Clínica",
            owner_user_id=agenda.alice.pk,
            timezone="America/Sao_Paulo",
            idempotency_key=uuid4().hex,
            source_app=source_app,
            source_path=source_path,
        )


def grants(agenda, calendar_id):
    with actor_tenant(agenda.admin, agenda.a):
        rows = AccessService().record_grants(actor=agenda.admin, table_key=TABLE, record_id=calendar_id)
    return {(row.group_id or row.membership_user_id, row.action) for row in rows}


def writable(agenda, calendar):
    with actor_tenant(agenda.bob, agenda.a):
        [info] = [row for row in CalendarService().calendars(actor=agenda.bob) if row.sysid == calendar.sysid]
    return info.writable


@pytest.fixture
def source(agenda):
    open_app(SOURCE)
    for organization in (agenda.a, agenda.b):
        activate_app(SOURCE, organization.pk)
    return SOURCE


def test_source_paths_stay_inside_the_source_app():
    assert valid_source_path("med", "") and valid_source_path("med", "apps/med/schedules/1/")
    for path in ("apps/other/1/", "apps/med/../x", "https://x.test/apps/med/", "apps/med//x", "apps/med\\x"):
        assert not valid_source_path("med", path), path


def test_a_manager_creates_a_shared_calendar_with_groups_and_people(agenda, login):
    client = login(agenda.admin)
    page = client.get(url(agenda, "calendars"))
    assert page.status_code == 200 and "Novo calendário compartilhado" in page.content.decode()
    key = re.search(r'name="idempotency_key" value="([0-9a-f]+)"', page.content.decode()).group(1)
    data = {
        "name": "Clínica",
        "owner": str(agenda.alice.pk),
        "timezone": "America/Sao_Paulo",
        "writer_groups": [str(agenda.group.pk)],
        "reader_users": [str(agenda.alice.pk)],
        "idempotency_key": key,
    }
    created = post(client, url(agenda, "calendars"), data)
    assert created.status_code == 302
    assert post(client, url(agenda, "calendars"), data).status_code == 302  # Resent: the same calendar.
    with actor_tenant(agenda.admin, agenda.a):
        [calendar] = Calendar.objects.filter(name="Clínica")
        assert calendar.visibility == "private" and calendar.owner_id == agenda.alice.pk
    assert created["Location"] == url(agenda, "calendar-detail", calendar)
    assert grants(agenda, calendar.pk) == {
        (agenda.group.pk, "read"),
        (agenda.group.pk, "update"),
        (agenda.alice.pk, "read"),
    }
    # Bob is in the writers group: he reads and writes the calendar now.
    with actor_tenant(agenda.bob, agenda.a):
        [info] = [row for row in CalendarService().calendars(actor=agenda.bob) if row.sysid == calendar.pk]
        assert info.writable


def test_a_member_without_access_manage_sees_no_form_and_gets_403(agenda, login):
    client = login(agenda.bob)
    page = client.get(url(agenda, "calendars"))
    assert page.status_code == 200 and "Novo calendário compartilhado" not in page.content.decode()
    data = {"name": "X", "owner": str(agenda.bob.pk), "timezone": "UTC", "idempotency_key": "k"}
    assert post(client, url(agenda, "calendars"), data).status_code == 403
    calendar = shared(agenda)
    with actor_tenant(agenda.admin, agenda.a):
        AccessService().grant_record_access(
            actor=agenda.admin,
            table_key=TABLE,
            record_id=calendar.sysid,
            action="read",
            membership_user_id=agenda.bob.pk,
        )
    detail = client.get(url(agenda, "calendar-detail", calendar)).content.decode()
    assert "Salvar compartilhamento" not in detail
    sharing = {"reader_users": [str(agenda.bob.pk)]}
    assert post(client, url(agenda, "calendar-sharing", calendar), sharing).status_code == 403


def test_changing_the_sharing_grants_and_revokes_on_the_next_request(agenda, login):
    calendar = shared(agenda)
    admin, bob = login(agenda.admin), login(agenda.bob)
    detail = url(agenda, "calendar-detail", calendar)
    assert bob.get(detail).status_code == 404
    page = admin.get(detail).content.decode()
    assert "Salvar compartilhamento" in page and "Renomear" in page
    assert (
        post(admin, url(agenda, "calendar-sharing", calendar), {"reader_users": [str(agenda.bob.pk)]}).status_code
        == 302
    )
    assert grants(agenda, calendar.sysid) == {(agenda.bob.pk, "read")}
    assert bob.get(detail).status_code == 200
    assert not writable(agenda, calendar)
    # Promoted to writer, then removed: update and read are revoked, the page is gone again.
    post(admin, url(agenda, "calendar-sharing", calendar), {"writer_users": [str(agenda.bob.pk)]})
    assert grants(agenda, calendar.sysid) == {(agenda.bob.pk, "read"), (agenda.bob.pk, "update")}
    assert writable(agenda, calendar)
    post(admin, url(agenda, "calendar-sharing", calendar), {})
    assert grants(agenda, calendar.sysid) == set()
    assert bob.get(detail).status_code == 404


def test_rename_and_delete_from_the_page(agenda, login):
    calendar = shared(agenda)
    admin = login(agenda.admin)
    stale = post(admin, url(agenda, "calendar-rename", calendar), {"name": "Nova", "revision": calendar.revision + 5})
    assert stale.status_code == 302
    post(admin, url(agenda, "calendar-rename", calendar), {"name": "Clínica Central", "revision": calendar.revision})
    with actor_tenant(agenda.admin, agenda.a):
        row = Calendar.objects.get(pk=calendar.sysid)
        assert row.name == "Clínica Central"
    deleted = post(admin, url(agenda, "calendar-delete", calendar), {"revision": row.revision})
    assert deleted.status_code == 302 and deleted["Location"] == url(agenda, "calendars")
    with actor_tenant(agenda.admin, agenda.a):
        assert not Calendar.objects.filter(pk=calendar.sysid).exists()


def test_a_calendar_with_events_is_not_deleted(agenda, login):
    from datetime import UTC, datetime, timedelta

    calendar = shared(agenda)
    start = datetime(2027, 3, 1, 9, tzinfo=UTC)
    with actor_tenant(agenda.alice, agenda.a):
        CalendarService().create_event(
            actor=agenda.alice,
            calendar_id=calendar.sysid,
            data={"title": "Consulta", "starts_at": start, "ends_at": start + timedelta(hours=1)},
        )
    response = post(login(agenda.admin), url(agenda, "calendar-delete", calendar), {"revision": calendar.revision})
    assert response.status_code == 302 and response["Location"] == url(agenda, "calendar-detail", calendar)
    with actor_tenant(agenda.admin, agenda.a):
        assert Calendar.objects.filter(pk=calendar.sysid).exists()


@pytest.mark.needs_example_app
def test_a_managed_calendar_shows_its_origin_and_refuses_changes(agenda, login, source):
    calendar = shared(agenda, source_app=source, source_path=PATH)
    assert calendar.source_app == source and calendar.source_path == PATH
    admin = login(agenda.admin)
    page = admin.get(url(agenda, "calendar-detail", calendar)).content.decode()
    assert "Gerenciado por" in page and "abrir na origem" in page
    assert f'href="/o/{agenda.a.pk}/{PATH}"' in page
    assert "Salvar compartilhamento" not in page and "Renomear" not in page and "Excluir calendário" not in page
    assert "Gerenciado por" in admin.get(url(agenda, "calendars")).content.decode()
    for name, data in (
        ("calendar-rename", {"name": "Outro", "revision": calendar.revision}),
        ("calendar-sharing", {"reader_users": [str(agenda.bob.pk)]}),
        ("calendar-delete", {"revision": calendar.revision}),
    ):
        assert post(admin, url(agenda, name, calendar), data).status_code == 409, name
    assert grants(agenda, calendar.sysid) == set()
    # The table gateway refuses the change too: only the source app's service changes it.
    from skeleton.contrib.calendar import services

    with actor_tenant(agenda.admin, agenda.a):
        row = Calendar.objects.get(pk=calendar.sysid)
        with pytest.raises(Exception, match="gerenciado"):
            services.update_calendar_record(row, user=agenda.admin, values={"name": "Outro"})
        with pytest.raises(Exception, match="gerenciado"):
            services.delete_calendar_record(row, user=agenda.admin)
        row.refresh_from_db()
        row.source_app = "skeleton_ledger"
        with pytest.raises(Exception, match="não muda"):
            row.save(update_fields={"source_app"})


@pytest.mark.needs_example_app
def test_creating_a_managed_calendar_validates_the_source(agenda, source):
    with actor_tenant(agenda.admin, agenda.a):
        for app, path in (("skeleton_calendar", ""), ("unknown_app", ""), (source, "apps/other/1/")):
            with pytest.raises(InvalidInput):
                CalendarService().create_shared_calendar(
                    actor=agenda.admin,
                    name="X",
                    owner_user_id=agenda.alice.pk,
                    timezone="UTC",
                    idempotency_key=uuid4().hex,
                    source_app=app,
                    source_path=path,
                )


@pytest.mark.needs_example_app
def test_an_app_claims_the_calendars_it_created_before_the_marker(agenda, source):
    calendar = shared(agenda)
    service = CalendarService()
    with actor_tenant(agenda.admin, agenda.a):
        with pytest.raises(Conflict) as stale:
            service.claim_calendar(
                actor=agenda.admin,
                calendar_id=calendar.sysid,
                source_app=source,
                expected_revision=calendar.revision + 1,
            )
        assert stale.value.code == "stale_revision"
        claimed = service.claim_calendar(
            actor=agenda.admin,
            calendar_id=calendar.sysid,
            source_app=source,
            source_path=PATH,
            expected_revision=calendar.revision,
        )
        assert claimed.source_app == source and claimed.revision == calendar.revision + 1
        # Idempotent: the same claim again changes nothing, whatever the revision.
        again = service.claim_calendar(
            actor=agenda.admin, calendar_id=calendar.sysid, source_app=source, source_path=PATH, expected_revision=0
        )
        assert again.revision == claimed.revision
    # Another app does not claim a calendar of someone else, not even the system.
    with system_context(reason="test:claim", organization_id=agenda.a.pk):
        with pytest.raises(Conflict) as foreign:
            service.claim_calendar(
                actor=None, calendar_id=calendar.sysid, source_app="skeleton_ledger", expected_revision=claimed.revision
            )
        assert foreign.value.code == "managed_calendar"


@pytest.mark.needs_example_app
def test_claim_is_authorized_and_only_for_shared_calendars(agenda, source):
    calendar = shared(agenda)
    service = CalendarService()
    with actor_tenant(agenda.bob, agenda.a), pytest.raises(NotFound):
        service.claim_calendar(actor=agenda.bob, calendar_id=calendar.sysid, source_app=source, expected_revision=1)
    with actor_tenant(agenda.alice, agenda.a), pytest.raises(Forbidden):
        # Alice owns and writes it, but has no access.manage.
        service.claim_calendar(actor=agenda.alice, calendar_id=calendar.sysid, source_app=source, expected_revision=1)
    with actor_tenant(agenda.admin, agenda.a):
        personal = service.my_calendars(actor=agenda.admin)[0]
        assert personal.kind == "personal"
        with pytest.raises(InvalidInput):
            service.claim_calendar(
                actor=agenda.admin, calendar_id=personal.sysid, source_app=source, expected_revision=personal.revision
            )
    with actor_tenant(agenda.bob, agenda.b), pytest.raises(NotFound):
        service.claim_calendar(actor=agenda.bob, calendar_id=calendar.sysid, source_app=source, expected_revision=1)


def test_calendar_links_for_a_reader_and_not_found_otherwise(agenda, login):
    calendar = shared(agenda)
    with actor_tenant(agenda.alice, agenda.a):
        links = CalendarService().calendar_links(actor=agenda.alice, calendar_id=calendar.sysid)
    assert links.calendar_path == f"apps/skeleton_calendar/calendars/{calendar.sysid}/"
    assert links.subscription_path == f"apps/skeleton_calendar/calendars/{calendar.sysid}/link/"
    base = reverse("web:dashboard", kwargs={"organization_id": agenda.a.pk})
    client = login(agenda.alice)
    assert base + links.calendar_path == url(agenda, "calendar-detail", calendar)
    assert client.get(base + links.calendar_path).status_code == 200
    assert client.get(base + links.subscription_path).status_code == 200
    with actor_tenant(agenda.bob, agenda.a), pytest.raises(NotFound):
        CalendarService().calendar_links(actor=agenda.bob, calendar_id=calendar.sysid)
    from skeleton.contrib.calendar.services import grant_member_access

    with actor_tenant(agenda.bob, agenda.b):
        grant_member_access(actor=agenda.bob)
    with actor_tenant(agenda.alice, agenda.b), pytest.raises(NotFound):
        CalendarService().calendar_links(actor=agenda.alice, calendar_id=calendar.sysid)


def test_organization_b_does_not_see_the_calendars_of_a(agenda, login):
    from skeleton.contrib.calendar.services import grant_member_access

    calendar = shared(agenda)
    with actor_tenant(agenda.bob, agenda.b):
        grant_member_access(actor=agenda.bob)
    bob = login(agenda.bob)  # Administrator of B.
    listed = bob.get(url(agenda, "calendars", organization=agenda.b)).content.decode()
    assert "Clínica" not in listed
    for name in ("calendar-detail", "calendar-link"):
        assert bob.get(url(agenda, name, calendar, organization=agenda.b)).status_code == 404
    for name in ("calendar-rename", "calendar-sharing", "calendar-delete"):
        assert post(bob, url(agenda, name, calendar, organization=agenda.b), {"revision": 1}).status_code == 404
    with actor_tenant(agenda.bob, agenda.b), pytest.raises(NotFound):
        AccessService().record_grants(actor=agenda.bob, table_key=TABLE, record_id=calendar.sysid)


def test_record_grants_needs_access_manage(agenda):
    calendar = shared(agenda)
    with actor_tenant(agenda.alice, agenda.a), pytest.raises(Forbidden):
        AccessService().record_grants(actor=agenda.alice, table_key=TABLE, record_id=calendar.sysid)
    with actor_tenant(agenda.admin, agenda.a), pytest.raises(InvalidInput):
        AccessService().record_grants(actor=agenda.admin, table_key="skeleton_calendar.event", record_id=calendar.sysid)
