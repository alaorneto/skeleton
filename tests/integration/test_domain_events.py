"""Domain events published in the write's transaction, validated against the TableSpec (#41).

Runs on PostgreSQL with the runtime role (tenant RLS of DomainEvent).
"""

import pytest
from django.core.exceptions import ValidationError

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import MissingTenantContext
from skeleton.events.core_events import CORE_EVENT_TYPES
from skeleton.events.models import DomainEvent
from skeleton.events.registry import EventType, event_problems, register_event_type
from skeleton.events.services import publish_event
from skeleton.tenancy.context import request_context, system_context, tenant_context
from skeleton.testing.registries import isolated_event_types

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PASSWORD = "Domain-events-Password!64"


@pytest.fixture(autouse=True)
def event_types(monkeypatch):
    with isolated_event_types(keep_consumers=True):
        register_event_type(EventType("notes.note_created", "example_notes.note", ("code", "title")))
        yield


@pytest.fixture
def tenants():
    alice, alpha = bootstrap_identity(email="ev-a@example.test", organization="ev-a", name="A", password=PASSWORD)
    bob, beta = bootstrap_identity(email="ev-b@example.test", organization="ev-b", name="B", password=PASSWORD)
    return alice, alpha, bob, beta


def note(organization, owner, code):
    from examples.notes.models import Note

    record = Note(organization=organization, owner=owner, code=code, title=f"Title {code}")
    record.save()
    return record


def events(organization, actor):
    with tenant_context(organization_id=organization.pk, actor=actor, source="test:events-read"):
        # Core lifecycle events (#43) of the fixture's memberships are not what these tests publish.
        rows = DomainEvent.objects.exclude(event_type__in=CORE_EVENT_TYPES).order_by("occurred_at", "sysid")
        return list(rows.values_list("event_type", "record_sysid", "data"))


def test_event_carries_declared_fields_and_correlation(tenants):
    alice, alpha, _, _ = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:events"):
        record = note(alpha, alice, "N-1")
        event = publish_event("notes.note_created", record=record, data={"code": record.code, "title": record.title})
    assert events(alpha, alice) == [("notes.note_created", record.pk, {"code": "N-1", "title": "Title N-1"})]
    assert (event.record_revision, event.actor_id, event.source, event.version) == (1, alice.pk, "test:events", 1)


def test_rollback_leaves_no_event(tenants):
    alice, alpha, _, _ = tenants
    with pytest.raises(RuntimeError), tenant_context(organization_id=alpha.pk, actor=alice, source="test:events-rb"):
        record = note(alpha, alice, "N-2")
        publish_event("notes.note_created", record=record, data={"code": "N-2"})
        raise RuntimeError("business failure")
    assert events(alpha, alice) == []


def test_publish_requires_tenant_and_same_organization(tenants):
    alice, alpha, bob, beta = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:events-own"):
        record = note(alpha, alice, "N-3")
    with request_context(actor=alice, request_id=None, source="test:events-global"):
        with pytest.raises(MissingTenantContext):
            publish_event("notes.note_created", record=record)
    with tenant_context(organization_id=beta.pk, actor=bob, source="test:events-cross"):
        with pytest.raises(ValidationError):
            publish_event("notes.note_created", record=record)
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:events-invalid"):
        with pytest.raises(ValidationError):
            publish_event("notes.note_created", record=record, data={"body": "secret"})
        with pytest.raises(ValidationError):
            publish_event("unknown.event", record=record)
        with pytest.raises(ValidationError):
            publish_event("notes.note_created", record=alice)
    with system_context(reason="test:events-system", organization_id=alpha.pk):
        publish_event("notes.note_created", record=record)
    assert len(events(alpha, alice)) == 1


def test_tenant_b_never_reads_events_of_a(tenants):
    alice, alpha, bob, beta = tenants
    with tenant_context(organization_id=alpha.pk, actor=alice, source="test:events-a"):
        publish_event("notes.note_created", record=note(alpha, alice, "N-4"))
    assert events(beta, bob) == []

    # RLS hides A's rows even from the unfiltered infrastructure manager.
    with tenant_context(organization_id=beta.pk, actor=bob, source="test:events-b-raw"):
        assert not DomainEvent.all_objects.filter(organization_id=alpha.pk).exists()


def test_declaring_a_sensitive_or_unknown_field_fails_the_check():
    register_event_type(EventType("notes.leaky", "example_notes.note", ("body",)))
    register_event_type(EventType("notes.unknown", "example_notes.note", ("missing",)))
    register_event_type(EventType("notes.table", "nope.nothing", ()))
    problems = "\n".join(event_problems())
    assert "notes.leaky: payload field body is sensitive" in problems
    assert "notes.unknown: payload field missing is not a read field" in problems
    assert "notes.table: unknown table" in problems
