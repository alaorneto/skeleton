"""AuditService of the core service API (#106).

Runs on PostgreSQL with the runtime role (RLS of AuditEvent).
"""

import pytest

from skeleton.service_api.audit import AuditService
from skeleton.service_api.errors import InvalidInput, NotFound
from skeleton.testing.service_api import (
    acting_as,
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"


def test_contract():
    assert_service_contract(AuditService)


@pytest.fixture
def scenario():

    from examples.notes.models import Note

    return build_service_scenario(Note, slug="history")


def edit(scenario, note, title):
    with acting_as(note.owner, note.organization):
        note.refresh_from_db()
        note.title = title
        note.save(update_fields={"title"})


@pytest.mark.needs_example_app
def test_history_is_authorized_redacted_and_most_recent_first(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="H-1", title="First", body="secret body")
    edit(scenario, note, "Second")
    service = AuditService()
    with acting_as(scenario.member, scenario.a) as actor:
        page = service.history(actor=actor, table_key=TABLE, record_id=note.pk)
    assert [entry.operation for entry in page.items] == ["update", "create"]
    created = {change.name: change for change in page.items[1].changes}
    assert created["body"].new == "[REDACTED]" and created["body"].redacted
    assert created["title"].new == "First"
    assert {change.name for change in page.items[0].changes} == {"title"}
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert len(service.history(actor=admin, table_key=TABLE, record_id=note.pk).items) == 2


@pytest.mark.needs_example_app
def test_without_audit_permission_or_record_access_is_not_found(scenario):
    note = scenario.create(scenario.a, owner=scenario.member, code="H-2", title="Mine")
    hidden = scenario.create(scenario.a, owner=scenario.admin_a, code="H-3", title="Private", visibility="private")
    service = AuditService()
    assert_denied_as_not_found(
        scenario.caller(
            lambda actor: service.history(actor=actor, table_key=TABLE, record_id=note.pk), user=scenario.denied
        ),
        missing=scenario.caller(
            lambda actor: service.history(
                actor=actor, table_key=TABLE, record_id="00000000-0000-0000-0000-000000000001"
            )
        ),
    )
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(NotFound):
            service.history(actor=actor, table_key=TABLE, record_id=hidden.pk)
        with pytest.raises(NotFound):
            service.history(actor=actor, table_key="sk_accounts.user", record_id=actor.pk)
        with pytest.raises(InvalidInput):
            service.history(actor=actor, table_key=TABLE, record_id="nope")
    assert_rejects_other_actor(
        scenario.caller(lambda actor: service.history(actor=scenario.denied, table_key=TABLE, record_id=note.pk))
    )


@pytest.mark.needs_example_app
def test_events_of_another_organization_never_appear(scenario):
    own = scenario.create(scenario.a, owner=scenario.member, code="H-4", title="A note")
    foreign = scenario.create(scenario.b, owner=scenario.member, code="H-5", title="B secret title")
    service = AuditService()
    assert_tenant_isolated(
        scenario.caller(lambda actor: service.history(actor=actor, table_key=TABLE, record_id=own.pk)),
        foreign=[foreign],
    )
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(NotFound):
        service.history(actor=actor, table_key=TABLE, record_id=foreign.pk)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        assert service.history(actor=admin, table_key=TABLE, record_id=foreign.pk).items == ()
