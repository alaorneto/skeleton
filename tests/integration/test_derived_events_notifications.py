"""Domain events, webhooks and notifications about a row of a derived table (#562).

Runs on PostgreSQL with the runtime role. The event and notification types point to
``WorkItem``, authorized through its ``Workspace`` (docs/access-control.md, "Tabelas derivadas").
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.fernet import Fernet
from django.test import override_settings

from skeleton.events.models import DomainEvent
from skeleton.events.registry import EventType, register_consumer, register_event_type
from skeleton.notifications.models import Notification, NotificationRecipient
from skeleton.notifications.registry import NotificationType, register_notification_type
from skeleton.service_api.errors import InvalidInput, NotFound
from skeleton.service_api.events import EventService
from skeleton.service_api.notifications import NotificationService, Recipients
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.registries import isolated_event_types, isolated_notification_types
from skeleton.testing.service_api import acting_as, build_service_scenario
from tests.integration.derived_support import child, derived_tables, grant

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
NOTES_MODULE = "examples.notes.services"
ITEM = "example_notes.workitem"
EVENT = "notes.item_closed"
KEY = Fernet.generate_key().decode()
RECEIVED = []


def as_module(function):
    namespace = {"__name__": NOTES_MODULE, "function": function}
    exec("def call(*args, **kwargs):\n    return function(*args, **kwargs)\n", namespace)  # noqa: S102
    return namespace["call"]


def capture(envelope):
    RECEIVED.append(envelope)


class Receiver(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.server.received.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def world(owner_schema_editor):
    RECEIVED.clear()
    with (
        derived_tables(owner_schema_editor) as tables,
        isolated_event_types(keep_consumers=True),
        isolated_notification_types(),
    ):
        register_event_type(EventType(EVENT, ITEM, ("status",), webhook=True, owner_app="example_notes"))
        register_consumer(EVENT, "tests.derived_capture", capture)
        register_notification_type(
            NotificationType(
                "notes.item_assigned",
                "Item atribuído",
                snapshot_fields=("title",),
                target_table_key=ITEM,
                owner_app="example_notes",
            )
        )
        register_notification_type(
            NotificationType(
                "notes.item_linked",
                "Item com link próprio",
                target_table_key=ITEM,
                owner_app="example_notes",
                open_url=lambda user, row: f"/own/{row.target_sysid}/",
            )
        )
        scenario = build_service_scenario(tables.workspace, slug="derived-events")
        shared = scenario.create(scenario.a, owner=scenario.admin_a, visibility="organization", title="shared")
        private = scenario.create(scenario.a, owner=scenario.admin_a, visibility="private", title="private")
        foreign = scenario.create(scenario.b, owner=scenario.member, visibility="organization", title="beta")
        items = {
            "shared": child(tables.item, scenario.a, workspace=shared, title="shared item"),
            "private": child(tables.item, scenario.a, workspace=private, title="private item"),
            "foreign": child(tables.item, scenario.b, workspace=foreign, title="beta item"),
        }
        yield tables, scenario, {"shared": shared, "private": private}, items


def test_publish_about_a_child_follows_the_anchor(world):
    tables, scenario, spaces, items = world
    publish = as_module(EventService().publish)
    with acting_as(scenario.member, scenario.a) as actor:
        receipt = publish(actor=actor, event_type=EVENT, record_id=items["shared"].pk, data=(("status", "closed"),))
        answers = []
        for name in ("private", "foreign"):
            with pytest.raises(NotFound) as denied:
                publish(actor=actor, event_type=EVENT, record_id=items[name].pk)
            answers.append((denied.value.code, denied.value.detail))
        assert answers[0] == answers[1]  # Not readable and other organization answer the same.
        with pytest.raises(InvalidInput):
            publish(actor=actor, event_type=EVENT, record_id=items["shared"].pk, data=(("title", "x"),))
    with system_context(reason="test:derived-event", organization_id=scenario.a.pk):
        event = DomainEvent.objects.get(pk=receipt.sysid)
        assert (event.table_key, event.record_sysid, event.data) == (ITEM, items["shared"].pk, {"status": "closed"})
        # In the system context of a consumer, the row only has to exist in the organization.
        as_module(EventService().publish)(actor=None, event_type=EVENT, record_id=items["private"].pk)
    run_pending_jobs()
    assert {envelope.record_sysid for envelope in RECEIVED} == {items["shared"].pk, items["private"].pk}
    assert all(envelope.table_key == ITEM for envelope in RECEIVED)


def test_rollback_leaves_no_event_and_no_notification(world):
    tables, scenario, spaces, items = world
    with pytest.raises(RuntimeError), acting_as(scenario.member, scenario.a) as actor:
        as_module(EventService().publish)(actor=actor, event_type=EVENT, record_id=items["shared"].pk)
        as_module(NotificationService().notify)(
            actor=actor,
            type_code="notes.item_assigned",
            recipients=Recipients(users=(scenario.admin_a.pk,)),
            target_table_key=ITEM,
            target_id=items["shared"].pk,
        )
        raise RuntimeError("caller failure")
    with system_context(reason="test:derived-rollback", organization_id=scenario.a.pk):
        assert not DomainEvent.objects.filter(table_key=ITEM).exists()
        assert not Notification.objects.filter(target_table_key=ITEM).exists()


def test_a_signed_webhook_delivers_the_child_event_with_identifiers_only(world):
    from skeleton.access.models import MemberRole, Role
    from skeleton.accounts.services import create_service_account
    from skeleton.events.services import publish_event
    from skeleton.integrations.models import WebhookDelivery
    from skeleton.integrations.webhooks.services import create_endpoint

    tables, scenario, spaces, items = world
    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.received = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with override_settings(
            DEBUG=True, SKELETON_OUTBOUND_HTTP_DEVELOPMENT_OVERRIDE=True, INTEGRATION_ENCRYPTION_KEYS=[KEY]
        ):
            with acting_as(scenario.admin_a, scenario.a) as admin:
                principal = create_service_account(actor=admin, name="Receiver")
                membership = principal.organization_memberships.get()
                role = Role.objects.get(code="service-contract")
                MemberRole(organization=scenario.a, membership=membership, role=role).save()
                create_endpoint(
                    actor=admin,
                    data={
                        "name": "Local",
                        "url": f"http://127.0.0.1:{server.server_port}/hook",
                        "principal": principal,
                        "event_types": [EVENT],
                    },
                )
            with acting_as(scenario.admin_a, scenario.a):
                publish_event(EVENT, record=items["shared"], data={"status": "closed"})
                publish_event(EVENT, record=items["private"], data={"status": "closed"})
            for _ in range(4):
                if not run_pending_jobs():
                    break
            with acting_as(scenario.admin_a, scenario.a):
                states = {row.record_sysid: row.state for row in WebhookDelivery.objects.all()}
    finally:
        server.shutdown()
        server.server_close()
    [body] = [json.loads(raw) for raw in server.received]
    assert body["data"] == {"table": ITEM, "record": str(items["shared"].pk)}
    assert "shared item" not in json.dumps(body)  # Identifiers only, never fields of the row.
    assert states == {items["shared"].pk: "succeeded", items["private"].pk: "unauthorized"}


def test_notifications_reach_only_readers_of_the_anchor(world):
    from skeleton.access.models import RecordGrant
    from skeleton.notifications.inbox import open_target

    tables, scenario, spaces, items = world
    grant(scenario, spaces["private"], user=scenario.member, action="read")
    notify = as_module(NotificationService().notify)
    everyone = Recipients(users=(scenario.member.pk, scenario.denied.pk))
    with acting_as(scenario.admin_a, scenario.a) as admin:
        for code in ("notes.item_assigned", "notes.item_linked"):
            notify(
                actor=admin, type_code=code, recipients=everyone, target_table_key=ITEM, target_id=items["private"].pk
            )
        with pytest.raises(NotFound):
            notify(
                actor=admin,
                type_code="notes.item_assigned",
                recipients=everyone,
                target_table_key=ITEM,
                target_id=items["foreign"].pk,
            )
    run_pending_jobs()
    with system_context(reason="test:derived-inbox", organization_id=scenario.a.pk):
        rows = {(row.user_id, row.type_code): row for row in NotificationRecipient.objects.all()}
    assert {user for user, _ in rows} == {scenario.member.pk}  # Not the member without the anchor's permission.
    with acting_as(scenario.member, scenario.a) as member:
        # No generic page for a derived table: no link, or the type's own one, never a 404.
        assert open_target(member, rows[(member.pk, "notes.item_assigned")].pk) is None
        assert open_target(member, rows[(member.pk, "notes.item_linked")].pk) == f"/own/{items['private'].pk}/"
    with system_context(reason="test:derived-revoke", organization_id=scenario.a.pk):
        for row in RecordGrant.objects.filter(record_sysid=spaces["private"].pk):
            row.delete()
    with acting_as(scenario.member, scenario.a) as member:
        assert open_target(member, rows[(member.pk, "notes.item_linked")].pk) is None  # Revoked: no target.
