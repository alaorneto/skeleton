"""NoteService through the contract kit (#100, #101): runs on PostgreSQL with the runtime role.

The PostgreSQL job runs this module with TEST_DATABASE_URL pointing at the runtime role.
"""

from uuid import uuid4

import pytest
from django.db import transaction

from examples.notes.models import Note
from examples.notes.service_api import NoteService, NoteSummary
from skeleton.service_api import Conflict, Forbidden, InvalidInput, Page, service_method
from skeleton.testing.service_api import (
    acting_as,
    assert_cas_conflict,
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

pytestmark = pytest.mark.needs_example_app


@pytest.fixture
def scenario(skeleton_db):
    return build_service_scenario(Note, slug="notes-service")


@pytest.fixture
def foreign(scenario):
    return scenario.create(scenario.b, code="foreign-code", title="Foreign title", body="foreign secret body")


def all_notes(actor, **filters):
    items, cursor = [], None
    while True:
        page = NoteService().list_notes(actor=actor, cursor=cursor, page_size=1, **filters)
        assert isinstance(page, Page)
        items.extend(page.items)
        if page.next_cursor is None:
            return items
        cursor = page.next_cursor


def test_note_service_passes_the_static_contract():
    assert_service_contract(NoteService)


def test_authorized_path_returns_dtos_without_the_sensitive_body(scenario):
    with scenario.as_member() as actor:
        created = NoteService().create_note(actor=actor, code="n-1", title="Primeira", body="secret body")
        assert isinstance(created, NoteSummary)
        assert (created.code, created.title, created.visibility, created.revision) == ("n-1", "Primeira", "private", 1)
        assert created.created_at.utcoffset().total_seconds() == 0
        assert not hasattr(created, "body")
        assert NoteService().get_note(actor=actor, note_id=created.sysid) == created
        assert NoteService().get_note(actor=actor, note_id=str(created.sysid)) == created
        assert [item.sysid for item in all_notes(actor)] == [created.sysid]
        assert [item.code for item in all_notes(actor, code="n-1")] == ["n-1"]
        assert NoteService().note_count(actor=actor) == 1
        changed = NoteService().update_note(
            actor=actor, note_id=created.sysid, expected_revision=created.revision, title="Alterada"
        )
        assert (changed.title, changed.revision) == ("Alterada", 2)
    with acting_as(scenario.member, scenario.a):
        assert Note.objects.get(pk=created.sysid).body == "secret body"


def test_default_deny_without_table_permission_or_record_access(scenario):
    hidden = scenario.create(scenario.a, owner=scenario.denied, visibility="private", code="hidden", title="Hidden")
    for call in (
        scenario.caller(lambda actor: NoteService().note_count(actor=actor), user=scenario.denied),
        scenario.caller(lambda actor: NoteService().list_notes(actor=actor), user=scenario.denied),
        scenario.caller(
            lambda actor: NoteService().create_note(actor=actor, code="x", title="x"), user=scenario.denied
        ),
    ):
        with pytest.raises(Forbidden):
            call()
    # The member has table permission but no access to a private note of someone else.
    assert_denied_as_not_found(
        scenario.caller(lambda actor: NoteService().get_note(actor=actor, note_id=hidden.pk)),
        missing=scenario.caller(lambda actor: NoteService().get_note(actor=actor, note_id=uuid4())),
    )
    assert_denied_as_not_found(
        scenario.caller(
            lambda actor: NoteService().update_note(
                actor=actor, note_id=hidden.pk, expected_revision=hidden.revision, title="taken"
            )
        )
    )
    with scenario.as_member() as actor:
        assert hidden.pk not in {item.sysid for item in all_notes(actor)}


def test_read_only_access_cannot_update(scenario):
    shared = scenario.create(scenario.a, owner=scenario.denied, visibility="organization", code="shared", title="S")
    call = scenario.caller(
        lambda actor: NoteService().update_note(
            actor=actor, note_id=shared.pk, expected_revision=shared.revision, title="taken"
        )
    )
    with pytest.raises(Forbidden):
        call()


def test_organizations_are_isolated(scenario, foreign):
    scenario.create(scenario.a, code="own", title="Own title")
    markers = [foreign, "foreign-code", "Foreign title"]
    assert_tenant_isolated(scenario.caller(lambda actor: all_notes(actor)), foreign=markers)
    assert_tenant_isolated(scenario.caller(lambda actor: NoteService().list_notes(actor=actor)), foreign=markers)
    assert_tenant_isolated(
        scenario.caller(lambda actor: NoteService().list_notes(actor=actor, code="foreign-code")), foreign=markers
    )
    assert scenario.caller(lambda actor: NoteService().note_count(actor=actor))() == 1
    assert_denied_as_not_found(
        scenario.caller(lambda actor: NoteService().get_note(actor=actor, note_id=foreign.pk)),
        missing=scenario.caller(lambda actor: NoteService().get_note(actor=actor, note_id=uuid4())),
    )
    assert_tenant_isolated(
        scenario.caller(
            lambda actor: NoteService().update_note(
                actor=actor, note_id=foreign.pk, expected_revision=foreign.revision, title="cross"
            )
        ),
        foreign=markers,
    )
    # The same member reads it inside B, so the isolation above is the tenant, not the grants.
    in_b = scenario.caller(
        lambda actor: NoteService().get_note(actor=actor, note_id=foreign.pk), organization=scenario.b
    )
    assert in_b().code == "foreign-code"


def test_disabled_app_is_unavailable_only_in_that_organization(scenario, foreign):
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: NoteService().note_count(actor=actor)), disabled=scenario.application_disabled
    )
    with scenario.application_disabled(scenario.b):
        assert scenario.caller(lambda actor: NoteService().note_count(actor=actor))() == 0
    assert scenario.caller(lambda actor: NoteService().note_count(actor=actor), organization=scenario.b)() == 1


def test_actor_must_match_the_context(scenario):
    assert_rejects_other_actor(scenario.caller(lambda actor: NoteService().note_count(actor=scenario.denied)))
    assert_rejects_other_actor(scenario.caller(lambda actor: NoteService().list_notes(actor=None)))


def test_cas_has_one_winner_and_never_retries(scenario):
    note = scenario.create(scenario.a, code="cas", title="Original")

    def update(title):
        return scenario.caller(
            lambda actor: NoteService().update_note(
                actor=actor, note_id=note.pk, expected_revision=note.revision, title=title
            )
        )

    winner = assert_cas_conflict(update("first"), update("second"))
    assert (winner.title, winner.revision) == ("first", note.revision + 1)
    assert scenario.caller(lambda actor: NoteService().get_note(actor=actor, note_id=note.pk))().title == "first"


def test_invalid_input_and_unique_code_conflict(scenario):
    with scenario.as_member() as actor:
        service = NoteService()
        service.create_note(actor=actor, code="dup", title="One")
        with pytest.raises(Conflict) as raised, transaction.atomic():
            service.create_note(actor=actor, code="dup", title="Two")
        assert raised.value.code == "integrity_conflict"
        for kwargs in ({"code": "", "title": "x"}, {"code": "y", "title": "t" * 201}, {"code": 7, "title": "x"}):
            with pytest.raises(InvalidInput), transaction.atomic():
                service.create_note(actor=actor, **kwargs)
        note = service.get_note(actor=actor, note_id=all_notes(actor)[0].sysid)
        with pytest.raises(InvalidInput):
            service.update_note(actor=actor, note_id=note.sysid, expected_revision=note.revision)
        with pytest.raises(InvalidInput):
            service.update_note(actor=actor, note_id=note.sysid, expected_revision="1", title="x")
        with pytest.raises(InvalidInput):
            service.get_note(actor=actor, note_id="not-a-uuid")
        with pytest.raises(InvalidInput):
            service.list_notes(actor=actor, page_size=101)


def test_caller_rollback_leaves_no_note_and_no_audit_event(scenario):
    from skeleton.audit.models import AuditEvent
    from skeleton.tenancy.context import system_context

    def events():
        with system_context(reason="test:notes-service-audit", organization_id=scenario.a.pk):
            return AuditEvent.objects.filter(model_label="example_notes.note").count()

    before = events()
    with scenario.as_member() as actor:
        with transaction.atomic():
            NoteService().create_note(actor=actor, code="rolled-back", title="Discarded")
            transaction.set_rollback(True)
        assert NoteService().list_notes(actor=actor, code="rolled-back").items == ()
    assert events() == before
    with scenario.as_member() as actor:
        NoteService().create_note(actor=actor, code="kept", title="Kept")
    assert events() == before + 1  # Positive control: the audit trigger is observable here.


def test_page_and_summary_use_the_service(scenario, client):
    scenario.create(scenario.a, code="visible", title="Visible")
    scenario.create(scenario.a, owner=scenario.denied, visibility="private", code="private", title="Private")
    client.force_login(scenario.member)
    response = client.get(f"/api/v1/o/{scenario.a.pk}/apps/example_notes/summary/")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    client.force_login(scenario.denied)
    assert client.get(f"/api/v1/o/{scenario.a.pk}/apps/example_notes/summary/").status_code == 403
    assert client.get(f"/o/{scenario.a.pk}/apps/example_notes/").status_code == 403


class LeakyNotes(NoteService):
    """Defective on purpose: reads past the tenant filter through the base manager."""

    @service_method(read_only=True, since="0.3.0")
    def every_title(self, *, actor: object) -> tuple[str, ...]:
        """Leaks.

        Autorização: nenhuma (defeito proposital).
        Erros: nenhum.
        Efeitos: nenhum.
        """
        return tuple(Note._base_manager.order_by("code").values_list("title", flat=True))


def test_kit_rejects_a_service_that_leaks_another_organization(scenario, foreign):
    # RLS of the runtime role hides B from A, so the leaky service returns nothing foreign here...
    assert_tenant_isolated(scenario.caller(lambda actor: LeakyNotes().every_title(actor=actor)), foreign=[foreign])
    # ...and the kit still rejects a result that carries another organization's data.
    with pytest.raises(AssertionError, match="leaks"):
        assert_tenant_isolated(lambda: ("Foreign title",), foreign=["Foreign title"])
