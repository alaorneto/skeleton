"""Core lifecycle events (#43): one event per effective membership or app activation change.

Runs on PostgreSQL as the runtime role, so the outbox rows are read back through RLS.
"""

import pytest
from django.db import transaction

from skeleton.events.core_events import (
    APP_DISABLED,
    APP_ENABLED,
    MEMBERSHIP_CREATED,
    MEMBERSHIP_DEACTIVATED,
)
from skeleton.events.registry import event_problems
from skeleton.testing.events import capture_events
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.usefixtures("skeleton_db")
APP = "example_notes"


@pytest.fixture
def access():
    return build_security_fixture()


def codes(events):
    return [(event.event_type, dict(event.data)) for event in events]


def test_core_event_types_are_declared_with_identifier_payloads():
    from skeleton.events.registry import get_event_type

    assert not event_problems()
    for code in (MEMBERSHIP_CREATED, MEMBERSHIP_DEACTIVATED):
        assert get_event_type(code).payload_fields == ("user",)
        assert get_event_type(code).webhook is False
    for code in (APP_ENABLED, APP_DISABLED):
        assert get_event_type(code).payload_fields == ("app_label",)


@pytest.mark.needs_example_app
def test_membership_services_emit_one_event_per_effective_change(access):
    from skeleton.access.services import create_membership, delete_membership, update_membership
    from skeleton.tenancy.models import Membership

    user = str(access.bob.pk)
    with actor_tenant(access.admin, access.a):
        with capture_events() as deactivated:
            update_membership(access.bob_membership, user=access.admin, data={"is_active": False})
        assert codes(deactivated) == [(MEMBERSHIP_DEACTIVATED, {"user": user})]
        assert deactivated[0].record_sysid == access.bob_membership.pk
        with capture_events() as unchanged:
            update_membership(Membership.objects.get(pk=access.bob_membership.pk), user=access.admin, data={})
            update_membership(
                Membership.objects.get(pk=access.bob_membership.pk), user=access.admin, data={"is_active": False}
            )
        assert unchanged == []
        with capture_events() as reactivated:
            update_membership(
                Membership.objects.get(pk=access.bob_membership.pk), user=access.admin, data={"is_active": True}
            )
        assert codes(reactivated) == [(MEMBERSHIP_CREATED, {"user": user})]
        with capture_events() as removed:
            delete_membership(Membership.objects.get(pk=access.bob_membership.pk), user=access.admin)
        assert codes(removed) == [(MEMBERSHIP_DEACTIVATED, {"user": user})]
        with capture_events() as restored:
            create_membership(user=access.admin, data={"user": access.bob})
        assert codes(restored) == [(MEMBERSHIP_CREATED, {"user": user})]
        with capture_events() as inactive_restore:
            delete_membership(Membership.objects.get(user=access.bob), user=access.admin)
            create_membership(user=access.admin, data={"user": access.bob, "is_active": False})
        assert codes(inactive_restore) == [(MEMBERSHIP_DEACTIVATED, {"user": user})]  # Restored inactive: no event.
        with capture_events() as repeated:
            create_membership(user=access.admin, data={"user": access.alice})  # Already active.
        assert repeated == []
        with capture_events() as new:
            create_membership(user=access.admin, data={"user": access.outsider})
        assert codes(new) == [(MEMBERSHIP_CREATED, {"user": str(access.outsider.pk)})]


@pytest.mark.needs_example_app
def test_provisioning_and_leaving_emit_events(access):
    from skeleton.access.models import Role
    from skeleton.access.services import leave_organization, provision_membership
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:core-events", organization_id=access.a.pk):
        member = Role.objects.get(code="member")
        with capture_events() as again:
            provision_membership(organization=access.a, user=access.alice, role=member)  # Already active.
        assert again == []
        with capture_events() as provisioned:
            provision_membership(organization=access.a, user=access.outsider, role=member)
        assert codes(provisioned) == [(MEMBERSHIP_CREATED, {"user": str(access.outsider.pk)})]
    with actor_tenant(access.alice, access.a):
        with capture_events() as left:
            leave_organization(access.alice)
        assert codes(left) == [(MEMBERSHIP_DEACTIVATED, {"user": str(access.alice.pk)})]


@pytest.mark.needs_example_app
def test_service_accounts_emit_membership_events(access):
    from skeleton.accounts.services import create_service_account, deactivate_service_account

    with actor_tenant(access.admin, access.a):
        with capture_events() as created:
            account = create_service_account(actor=access.admin, name="Integração")
        assert codes(created) == [(MEMBERSHIP_CREATED, {"user": str(account.pk)})]
        with capture_events() as deactivated:
            deactivate_service_account(actor=access.admin, user=account)
            deactivate_service_account(actor=access.admin, user=account)  # Second time: no effect.
        assert codes(deactivated) == [(MEMBERSHIP_DEACTIVATED, {"user": str(account.pk)})]


@pytest.mark.needs_example_app
def test_app_activation_emits_only_on_enabled_changes(access):
    from skeleton.system.applications import application_state, configure_application
    from skeleton.testing.applications import open_app

    open_app(APP)

    def configure(enabled, configuration=None):
        state = application_state(APP)
        configure_application(
            APP,
            user=access.admin,
            enabled=enabled,
            configuration=state["configuration"] if configuration is None else configuration,
            revision=state["revision"],
        )

    with actor_tenant(access.admin, access.a):
        configure(False)  # Fixture already enabled it: back to off, outside the capture.
        with capture_events() as first:
            configure(False)  # Already off: configuration-only change.
            configure(True)
            configure(True, {"heading": "Novo título"})  # Configuration only: no event.
            configure(False)
        assert codes(first) == [(APP_ENABLED, {"app_label": APP}), (APP_DISABLED, {"app_label": APP})]


@pytest.mark.needs_example_app
def test_rollback_leaves_no_event_and_tenants_are_isolated(access):
    from skeleton.access.services import update_membership
    from skeleton.events.models import DomainEvent
    from skeleton.tenancy.models import Membership

    with actor_tenant(access.admin, access.a):
        before = DomainEvent.objects.filter(event_type=MEMBERSHIP_DEACTIVATED).count()
        with pytest.raises(RuntimeError), transaction.atomic():
            update_membership(access.bob_membership, user=access.admin, data={"is_active": False})
            raise RuntimeError("caller rolls back")
        assert DomainEvent.objects.filter(event_type=MEMBERSHIP_DEACTIVATED).count() == before
        fresh = Membership.objects.get(pk=access.bob_membership.pk)
        assert fresh.is_active  # The rolled back change left the membership as it was.
        update_membership(fresh, user=access.admin, data={"is_active": False})
        assert DomainEvent.objects.filter(event_type=MEMBERSHIP_DEACTIVATED).count() == before + 1
    with actor_tenant(access.bob, access.b):
        # RLS and the tenant manager: B never reads A's lifecycle events.
        assert not DomainEvent.objects.filter(record_sysid=access.bob_membership.pk).exists()


@pytest.mark.needs_example_app
def test_core_events_enqueue_no_webhook_job(access):
    from skeleton.access.services import update_membership
    from skeleton.events.registry import consumers_for
    from skeleton.jobs.models import Job

    assert consumers_for(MEMBERSHIP_CREATED) == ()
    # The only core consumer of app.enabled reschedules the flows of that app (docs/flows.md); never the webhooks.
    assert [consumer.code for consumer in consumers_for(APP_ENABLED)] == ["flows.resume_application"]
    before = Job.all_objects.filter(job_type="events.consume").count()
    with actor_tenant(access.admin, access.a):
        update_membership(access.bob_membership, user=access.admin, data={"is_active": False})
    assert Job.all_objects.filter(job_type="events.consume").count() == before
