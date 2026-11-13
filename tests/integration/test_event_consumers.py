"""Event delivery: one job per event and consumer, retries in isolation, retention (#42)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity
from skeleton.events.consumers import consume
from skeleton.events.core_events import CORE_EVENT_TYPES
from skeleton.events.models import DomainEvent
from skeleton.events.registry import EventType, event_problems, register_consumer, register_event_type
from skeleton.events.retention import purge_expired_events
from skeleton.events.services import publish_event
from skeleton.jobs.models import Job
from skeleton.tenancy.context import system_context, tenant_context
from skeleton.testing.events import capture_events
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_event_types

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
PASSWORD = "Event-consumers-Password!81"
seen = {"stable": set(), "flaky": [], "envelopes": []}


def stable(envelope):
    seen["stable"].add(envelope.event_id)
    seen["envelopes"].append(envelope)


def flaky(envelope):
    seen["flaky"].append(envelope.event_id)
    if len(seen["flaky"]) == 1:
        raise RuntimeError("consumer outage")


@pytest.fixture(autouse=True)
def declarations(monkeypatch):

    seen["stable"].clear()
    seen["flaky"].clear()
    seen["envelopes"].clear()
    with isolated_event_types():
        register_event_type(EventType("notes.note_created", "example_notes.note", ("code",)))
        register_consumer("notes.note_created", "test.stable", stable)
        # The wildcard narrowed to the test types: the fixture's membership events (#43) stay out.
        register_consumer("*", "test.flaky", flaky, accepts=lambda event_type: event_type.code.startswith("notes."))
        yield


@pytest.fixture
def tenant():
    user, organization = bootstrap_identity(
        email="consumers@example.test", organization="consumers", name="Consumers", password=PASSWORD
    )
    return user, organization


def publish(user, organization, code="C-1"):
    from examples.notes.models import Note

    with tenant_context(organization_id=organization.pk, actor=user, source="test:consumers"):
        note = Note(organization=organization, owner=user, code=code, title=code)
        note.save()
        with capture_events() as captured:
            event = publish_event("notes.note_created", record=note, data={"code": code})
    assert captured == [event]
    return event


def make_due():
    Job.all_objects.filter(state=Job.State.QUEUED).update(run_after=timezone.now() - timedelta(seconds=1))


def test_each_consumer_gets_its_own_job_and_a_failure_is_retried_alone(tenant):
    user, organization = tenant
    event = publish(user, organization)
    keys = set(Job.all_objects.filter(job_type="events.consume").values_list("idempotency_key", flat=True))
    assert keys == {f"event:{event.pk}:test.stable", f"event:{event.pk}:test.flaky"}
    states = sorted(outcome.state for outcome in run_pending_jobs())
    assert states == ["queued", "succeeded"]
    assert seen["stable"] == {event.pk}
    make_due()
    assert [outcome.state for outcome in run_pending_jobs()] == ["succeeded"]
    assert seen["flaky"] == [event.pk, event.pk]
    envelope = seen["envelopes"][0]
    assert (envelope.organization_id, envelope.actor_id, dict(envelope.data)) == (
        organization.pk,
        user.pk,
        {"code": "C-1"},
    )
    with pytest.raises(TypeError):
        envelope.data["code"] = "changed"


def test_redelivery_is_harmless_for_an_idempotent_consumer(tenant):
    user, organization = tenant
    event = publish(user, organization, "C-2")
    run_pending_jobs()
    with system_context(reason="test:redeliver", organization_id=organization.pk):
        consume(event_id=str(event.pk), consumer="test.stable")
        from skeleton.events.consumers import enqueue_deliveries

        enqueue_deliveries(event)  # Same keys: the existing jobs are returned, nothing new.
    assert seen["stable"] == {event.pk}
    assert Job.all_objects.filter(job_type="events.consume").count() == 2


def test_consumer_of_unknown_event_fails_the_check():
    register_consumer("notes.missing", "test.orphan", stable)
    assert "consumer test.orphan: unknown event notes.missing" in "\n".join(event_problems())


def test_retention_deletes_only_expired_events(tenant):
    user, organization = tenant
    fresh = publish(user, organization, "C-3")
    with system_context(reason="test:retention-seed", organization_id=organization.pk):
        old = DomainEvent(
            organization_id=organization.pk,
            event_type="notes.note_created",
            table_key="example_notes.note",
            record_sysid=fresh.record_sysid,
            source="test",
            occurred_at=timezone.now() - timedelta(days=31),
        )
        old.save()
        assert purge_expired_events() == 1
        remaining = DomainEvent.objects.exclude(event_type__in=CORE_EVENT_TYPES)  # Fixture memberships (#43).
        assert list(remaining.values_list("pk", flat=True)) == [fresh.pk]
        # A delivery of a purged event is a no-op.
        consume(event_id=str(old.pk), consumer="test.stable")
    assert seen["stable"] == set()


def test_event_retention_deletes_in_batches_and_only_its_organization(tenant):
    """Batched deletes keep the organization filter of the manager (#308)."""
    from skeleton.accounts.services import bootstrap_identity

    user, organization = tenant
    other_user, other = bootstrap_identity(
        email="events-b@example.test", organization="events-b", name="B", password="Events-B-Password-2026!"
    )
    old = timezone.now() - timedelta(days=31)

    def seed(org, count):
        with system_context(reason="test:retention-batches", organization_id=org.pk):
            for _ in range(count):
                DomainEvent(
                    organization_id=org.pk,
                    event_type="notes.note_created",
                    table_key="example_notes.note",
                    record_sysid=org.pk,
                    source="test",
                    occurred_at=old,
                ).save()

    seed(organization, 3)
    seed(other, 2)
    with system_context(reason="test:retention-a", organization_id=organization.pk):
        assert purge_expired_events() == 3
    with system_context(reason="test:retention-b", organization_id=other.pk):
        assert DomainEvent.objects.filter(occurred_at__lt=timezone.now() - timedelta(days=30)).count() == 2
