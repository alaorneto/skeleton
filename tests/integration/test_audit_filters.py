"""Audit filters by period, actor, operation and request (#184), never wider than the authorization."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils import timezone

from skeleton.service_api.audit import AuditLogService
from skeleton.service_api.errors import Forbidden, InvalidInput
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, assert_service_contract, build_service_scenario
from tests import support

pytestmark = pytest.mark.usefixtures("skeleton_db")
SERVICE = AuditLogService()


def test_contract():
    assert_service_contract(AuditLogService)


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    return build_service_scenario(Note, slug="audit-filters")


def write(user, organization, code, *, request_id=None, title="Título"):
    """Create then update one note as ``user`` in one request, so both events share its id."""
    from examples.notes.models import Note

    request_id = request_id or uuid4()
    with request_context(actor=user, request_id=request_id, source="test:audit-filters"):
        with tenant_context(organization_id=organization.pk, actor=user, source="test:audit-filters"):
            note = Note(organization=organization, owner=user, visibility="organization", code=code, title=title)
            note.save()
            note.title = title + " 2"
            note.save(update_fields={"title"})
    return note, request_id


def codes(page):
    return sorted({item.record_sysid for item in page.items})


@pytest.mark.needs_example_app
def test_filters_narrow_by_actor_operation_request_and_period(scenario):
    member_note, member_request = write(scenario.member, scenario.a, "M-1")
    admin_note, _ = write(scenario.admin_a, scenario.a, "A-1")
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert codes(SERVICE.events(actor=admin, model_label="example_notes.note", actor_id=scenario.member.pk)) == [
            member_note.pk
        ]
        updates = SERVICE.events(actor=admin, model_label="example_notes.note", operation="update")
        assert {item.operation for item in updates.items} == {"update"} and updates.count == 2
        same_request = SERVICE.events(actor=admin, request_id=member_request)
        assert {item.record_sysid for item in same_request.items} == {member_note.pk} and same_request.count == 2
        now = timezone.now()
        assert SERVICE.events(actor=admin, since=now + timedelta(minutes=1)).count == 0
        assert SERVICE.events(actor=admin, until=now - timedelta(days=1)).count == 0
        recent = SERVICE.events(actor=admin, model_label="example_notes.note", since=now - timedelta(minutes=5))
        assert {item.record_sysid for item in recent.items} == {member_note.pk, admin_note.pk}
        with pytest.raises(InvalidInput):
            SERVICE.events(actor=admin, operation="truncate")
        with pytest.raises(InvalidInput):
            SERVICE.events(actor=admin, actor_id="not-a-uuid")


@pytest.mark.needs_example_app
def test_filters_never_reach_another_organization(scenario):
    _, request_a = write(scenario.admin_a, scenario.a, "A-1")
    with acting_as(scenario.admin_b, scenario.b) as admin_b:
        assert SERVICE.events(actor=admin_b, request_id=request_a).count == 0
        assert SERVICE.events(actor=admin_b, actor_id=scenario.admin_a.pk).count == 0


@pytest.mark.needs_example_app
def test_a_member_without_audit_permission_filters_only_what_they_already_see(scenario):
    _, request_a = write(scenario.admin_a, scenario.a, "A-1")
    with acting_as(scenario.denied, scenario.a) as denied:
        assert SERVICE.events(actor=denied, request_id=request_a).count == 0


@pytest.mark.needs_example_app
def test_related_domain_events_only_for_the_administrator(scenario):
    from skeleton.events.models import DomainEvent

    request_id = uuid4()
    with system_context(reason="test:audit-domain-event", organization_id=scenario.a.pk):
        DomainEvent(
            organization_id=scenario.a.pk,
            event_type="example_notes.note.created",
            table_key="example_notes.note",
            record_sysid=uuid4(),
            request_id=request_id,
            source="test",
            data={"secret": "never shown"},
        ).save()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        [event] = SERVICE.related_domain_events(actor=admin, request_id=request_id)
        assert event.event_type == "example_notes.note.created" and not hasattr(event, "data")
    with acting_as(scenario.admin_b, scenario.b) as admin_b:
        assert SERVICE.related_domain_events(actor=admin_b, request_id=request_id) == ()
    with acting_as(scenario.member, scenario.a) as member, pytest.raises(Forbidden):
        SERVICE.related_domain_events(actor=member, request_id=request_id)


def login(user):
    return support.login(user, password=FIXTURE_PASSWORD, csrf=False)


@pytest.mark.needs_example_app
def test_web_and_api_accept_the_filters_and_refuse_invalid_ones(scenario):
    member_note, request_id = write(scenario.member, scenario.a, "M-1")
    client = login(scenario.admin_a)
    page = reverse("web:audit", kwargs={"organization_id": scenario.a.pk})
    response = client.get(page, {"request_id": str(request_id), "operation": "create"})
    assert response.status_code == 200 and str(member_note.pk) in response.content.decode()
    assert client.get(page, {"operation": "truncate"}).status_code == 400
    assert client.get(page, {"from": "ontem"}).status_code == 400
    assert client.get(page, {"unknown": "1"}).status_code == 400
    api = reverse("api:audit", kwargs={"organization_id": scenario.a.pk})
    data = client.get(api, {"actor": str(scenario.member.pk), "from": timezone.localdate().isoformat()}).json()
    assert {item["record_sysid"] for item in data["results"]} == {str(member_note.pk)}
    assert client.get(api, {"request_id": "x"}).status_code == 400


@pytest.mark.needs_example_app
def test_platform_audit_accepts_the_filters(scenario):
    from skeleton.accounts.services import bootstrap_identity

    admin, _ = bootstrap_identity(
        email="platform-audit@example.test", organization="platform-audit", name="Plataforma", password=FIXTURE_PASSWORD
    )
    client = login(admin)
    url = reverse("platform:audit")
    assert client.get(url, {"operation": "create", "from": "2026-01-01"}).status_code == 200
    assert client.get(url, {"operation": "truncate"}).status_code == 400
