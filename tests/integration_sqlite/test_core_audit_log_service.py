"""AuditLogService: the audit log behind /o/<org>/audit/ and /platform/audit/ (#110).

Runs on SQLite and, in the PostgreSQL job, with the runtime role (RLS of AuditEvent).
"""

import json

import pytest

from skeleton.service_api.audit import AuditLogService
from skeleton.service_api.errors import Forbidden, InvalidInput, NotFound
from skeleton.testing.service_api import (
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"


def test_contract():
    assert_service_contract(AuditLogService)


@pytest.fixture
def scenario():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="audit-log")


def test_events_are_filtered_before_counting_and_redacted(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="L-1", title="Visible", body="secret body")
    hidden = scenario.create(scenario.a, owner=scenario.admin_a, code="L-2", title="Hidden", visibility="private")
    service = AuditLogService()
    with acting_as(scenario.member, scenario.a) as actor:
        log = service.events(actor=actor, model_label=TABLE)
    assert log.count == 1 and [event.record_sysid for event in log.items] == [note.pk]
    changes = json.loads(log.items[0].changes_json)
    assert changes["title"]["new"] == "Visible"
    assert "secret body" not in log.items[0].changes_json
    with acting_as(scenario.admin_a, scenario.a) as admin:
        everything = service.events(actor=admin, model_label=TABLE)
        assert {event.record_sysid for event in everything.items} == {note.pk, hidden.pk}
        only = service.events(actor=admin, record_id=str(hidden.pk))
        assert [event.record_sysid for event in only.items] == [hidden.pk]
        assert service.event(actor=admin, event_id=only.items[0].sysid).record_sysid == hidden.pk


def test_default_deny_and_hidden_events_answer_not_found(scenario):
    note = scenario.create(scenario.a, owner=scenario.admin_a, code="L-3", title="Private", visibility="private")
    service = AuditLogService()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        event_id = service.events(actor=admin, record_id=note.pk).items[0].sysid
    with acting_as(scenario.denied, scenario.a) as denied:
        assert service.events(actor=denied).count == 0
        with pytest.raises(NotFound):
            service.event(actor=denied, event_id=event_id)
    with acting_as(scenario.member, scenario.a) as member:
        with pytest.raises(NotFound):
            service.event(actor=member, event_id=event_id)
        with pytest.raises(NotFound):
            service.event(actor=member, event_id="00000000-0000-0000-0000-000000000001")


def test_pages_and_invalid_input(scenario):
    for number in range(3):
        scenario.create(scenario.a, owner=scenario.member, code=f"P-{number}", title=f"Page {number}")
    service = AuditLogService()
    with acting_as(scenario.admin_a, scenario.a) as admin:
        first = service.events(actor=admin, model_label=TABLE, page_size=2)
        second = service.events(actor=admin, model_label=TABLE, page=2, page_size=2)
        assert (first.count, first.page, len(first.items), len(second.items)) == (3, 1, 2, 1)
        assert {event.sysid for event in first.items}.isdisjoint(event.sysid for event in second.items)
        assert service.events(actor=admin, model_label=TABLE, page=9, page_size=2).items == ()
        for arguments in ({"page": 0}, {"page": "1"}, {"page_size": 101}, {"record_id": "nope"}, {"model_label": 1}):
            with pytest.raises(InvalidInput):
                service.events(actor=admin, **arguments)
        with pytest.raises(InvalidInput):
            service.event(actor=admin, event_id="nope")
    assert_rejects_other_actor(scenario.caller(lambda actor: service.events(actor=scenario.denied)))


def test_events_of_another_organization_never_appear(scenario):
    scenario.create(scenario.a, owner=scenario.member, code="L-4", title="A note")
    foreign = scenario.create(scenario.b, owner=scenario.member, code="L-5", title="B secret title")
    service = AuditLogService()
    with acting_as(scenario.admin_b, scenario.b) as admin_b:
        foreign_event = service.events(actor=admin_b, record_id=foreign.pk).items[0].sysid
    assert_tenant_isolated(
        scenario.caller(lambda actor: service.events(actor=actor, page_size=100), user=scenario.admin_a),
        foreign=[foreign],
    )
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert service.events(actor=admin, record_id=foreign.pk).count == 0
        with pytest.raises(NotFound):
            service.event(actor=admin, event_id=foreign_event)


def test_without_organization_only_the_platform_reads_global_events(scenario):
    from uuid import uuid4

    from skeleton.tenancy.context import request_context

    service = AuditLogService()
    with request_context(actor=scenario.admin_a, request_id=uuid4(), source="test:service-api"):
        with pytest.raises(Forbidden):
            service.events(actor=scenario.admin_a)
