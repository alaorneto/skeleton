"""Each command of the service API runs in a savepoint of the caller's transaction (#232)."""

import pytest
from django.db import connection

from skeleton.service_api import Conflict, ServiceAPI, service_method
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app


class Writer(ServiceAPI):
    app_label = "example_notes"

    @service_method(read_only=False, since="1.0")
    def duplicate(self, *, actor, code: str) -> int:
        """Two notes with the same code: the second violates the unique constraint."""
        from examples.notes.models import Note
        from skeleton.tenancy.context import current_context

        organization = current_context().organization_id
        Note(organization_id=organization, owner=actor, code=code, title="first", body="").save()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO skeleton.example_notes_note (sysid, organization_id, owner_id, code, title, body, visibility,"
                " created_at, updated_at, revision) SELECT gen_random_uuid(), organization_id, owner_id, code, title, body,"
                " visibility, created_at, updated_at, 1 FROM skeleton.example_notes_note WHERE code = %s",
                [code],
            )
        return 1


def as_module(module, function):
    """Call ``function`` from a frame of ``module``, so the service sees that app as the caller."""
    namespace = {"__name__": module, "function": function}
    exec("def call(*args, **kwargs):\n    return function(*args, **kwargs)\n", namespace)  # noqa: S102
    return namespace["call"]


@pytest.fixture
def records(skeleton_db):
    assert connection.vendor == "postgresql"
    return build_security_fixture()


def test_integrity_error_inside_a_command_leaves_the_transaction_usable(records):
    from examples.notes.models import Note

    with actor_tenant(records.alice, records.a):
        with pytest.raises(Conflict):
            Writer().duplicate(actor=records.alice, code="atomic-dup")
        assert not Note.objects.filter(code="atomic-dup").exists()  # The next query works.


def test_failed_fan_out_leaves_no_notification(records, monkeypatch):
    from skeleton.jobs import services as jobs
    from skeleton.notifications.models import Notification
    from skeleton.notifications.registry import NotificationType, register_notification_type
    from skeleton.service_api.notifications import NotificationService, Recipients
    from skeleton.testing.registries import isolated_notification_types

    def broken_enqueue(*args, **kwargs):
        raise ValueError("fan-out refused")

    with isolated_notification_types():
        register_notification_type(
            NotificationType("notes.atomic", "Teste", target_table_key="example_notes.note", owner_app="example_notes")
        )
        monkeypatch.setattr(jobs, "enqueue", broken_enqueue)
        with actor_tenant(records.alice, records.a):
            before = Notification.objects.count()
            notify = as_module("examples.notes.services", NotificationService().notify)
            with pytest.raises(Exception):  # noqa: B017 - the public error class does not matter here.
                notify(
                    actor=records.alice,
                    type_code="notes.atomic",
                    recipients=Recipients(users=(records.bob.pk,)),
                    target_table_key="example_notes.note",
                    target_id=records.private.pk,
                )
            assert Notification.objects.count() == before
