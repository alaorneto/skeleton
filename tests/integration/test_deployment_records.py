from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.test import Client

from skeleton.accounts.services import bootstrap_identity
from skeleton.common.exceptions import ConcurrentUpdate, ImmutableRecord
from skeleton.system.models import ComponentInstallation, DeploymentOperation
from skeleton.tenancy.context import request_context, system_context

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Deployment-test-password!49"


def actor(*, platform=False):
    user, _ = bootstrap_identity(
        email="release-actor@example.test", organization="releases", name="Releases", password=PASSWORD
    )
    if not platform:
        with system_context(reason="test:platform"):
            user.is_staff = False
            user.is_superuser = False
            user.save(update_fields={"is_staff", "is_superuser"})
    return user


def agent_status(row, *, state="running", stage="backup_ready", **values):
    return {
        "id": str(row.pk),
        "operation": row.operation,
        "action": row.operation,
        "expected_generation": row.expected_generation,
        "generation": row.expected_generation,
        "plan_digest": row.plan_digest,
        "state": state,
        "stage": stage,
        **values,
    }


def test_deployment_plan_and_result_are_redacted_before_audit_persistence():
    from skeleton.audit.models import AuditEvent

    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        row = DeploymentOperation(
            operation="install",
            expected_generation=0,
            plan_digest="a" * 64,
            plan={"private_path": "/private/release", "credential": "never-audit"},
        )
        row.save()
        row.result = {"secret_status_ticket": "not-audit-either"}
        row.state = "approved"
        row.save(update_fields={"result", "state"})
        events = list(AuditEvent.objects.filter(record_sysid=row.pk).order_by("record_revision"))
        assert events[0].actor_sysid == user.pk
        assert events[0].changes["plan"]["new"] == "[REDACTED]"
        assert events[1].changes["result"]["new"] == "[REDACTED]"
        assert events[1].changes["state"]["new"] == "approved"


def test_deployment_plan_is_immutable_and_concurrent_approval_has_one_winner():
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        row = DeploymentOperation(
            operation="update", expected_generation=2, plan_digest="b" * 64, plan={"generation": 2}
        )
        row.save()
        stale = DeploymentOperation.objects.get(pk=row.pk)
        row.state = "approved"
        row.save(update_fields={"state"})
        stale.state = "approved"
        with pytest.raises(ConcurrentUpdate):
            stale.save(update_fields={"state"})
        row.plan = {"generation": 99}
        with pytest.raises(ImmutableRecord):
            row.save(update_fields={"plan"})
        assert DeploymentOperation.objects.get(pk=row.pk).plan == {"generation": 2}


def test_component_identity_cannot_be_reassigned_to_another_app():
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        row = ComponentInstallation(
            component_id="tasks",
            kind="app",
            app_label="project_tasks",
            package_name="tasks",
            version="1.0.0",
            digest="c" * 64,
            publisher="trusted",
            generation=1,
            state="installed",
        )
        row.save()
        row.app_label = "sk_accounts"
        with pytest.raises(ImmutableRecord):
            row.save(update_fields={"app_label"})
        assert ComponentInstallation.objects.get(pk=row.pk).app_label == "project_tasks"


def test_organization_admin_cannot_use_platform_updates():
    user = actor()
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
    assert client.get("/platform/updates/").status_code == 403
    assert client.post("/platform/updates/plan/", {"action": "uninstall", "app_ids": "tasks"}).status_code == 403
    assert client.get("/admin/system/releases/").status_code == 404


@pytest.mark.needs_example_app
def test_retained_app_can_only_disable_existing_configuration():
    from django.core.exceptions import ValidationError

    from skeleton.system.app_registry import get_app, register_app, unregister_app
    from skeleton.system.models import AppInstallation

    user, organization = bootstrap_identity(
        email="retained@example.test", organization="retained", name="Retained", password=PASSWORD
    )
    manifest = get_app("example_notes")
    with system_context(reason="test:retention"):
        ComponentInstallation(
            component_id="example-notes",
            kind="app",
            app_label="example_notes",
            package_name="notes",
            version="0.1.0",
            state="schema_retained",
        ).save()
    with system_context(reason="test:retention", organization_id=organization.pk):
        row = AppInstallation.objects.get(app_label="example_notes")  # Activated by the test precondition.
        row.configuration = {"heading": "Preserved"}
        row.save(update_fields={"configuration"})
    unregister_app("example_notes")
    try:
        with system_context(reason="test:retention", organization_id=organization.pk):
            AppInstallation.objects.filter(pk=row.pk).update(enabled=False)
            row = AppInstallation.objects.get(pk=row.pk)
            assert row.configuration == {"heading": "Preserved"}
            assert row.enabled is False
            row.enabled = True
            with pytest.raises(ValidationError):
                row.save(update_fields={"enabled"})
            row = AppInstallation.objects.get(pk=row.pk)
            row.enabled = False
            row.configuration = {"heading": "Changed"}
            with pytest.raises(ValidationError):
                row.save(update_fields={"configuration"})
    finally:
        register_app(manifest)


def test_private_maintenance_smoke_does_not_bypass_authorization_or_writes(tmp_path):
    from django.test import override_settings

    marker, token_file = tmp_path / "maintenance", tmp_path / "smoke-token"
    token = "maintenance-smoke-private-token-32-chars"
    token_file.write_text(token)
    user = actor()
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
    marker.touch()
    with override_settings(
        SKELETON_MAINTENANCE_MARKER=str(marker),
        SKELETON_DEPLOYMENT_SMOKE_TOKEN_FILE=str(token_file),
        SKELETON_DEPLOYMENT_SMOKE_PATHS=("/accounts/login/", "/platform/updates/"),
    ):
        client = Client()
        assert client.get("/accounts/login/").status_code == 503
        assert client.get("/accounts/login/", HTTP_X_SKELETON_DEPLOYMENT_SMOKE="wrong").status_code == 503
        assert client.get("/accounts/login/", HTTP_X_SKELETON_DEPLOYMENT_SMOKE=token).status_code == 200
        assert client.post("/accounts/login/", {}, HTTP_X_SKELETON_DEPLOYMENT_SMOKE=token).status_code == 503
        assert client.get("/profile/", HTTP_X_SKELETON_DEPLOYMENT_SMOKE=token).status_code == 503
        marker.unlink()
        assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
        marker.touch()
        assert client.get("/platform/updates/", HTTP_X_SKELETON_DEPLOYMENT_SMOKE=token).status_code == 403


def test_release_upload_is_bounded_and_still_requires_csrf(tmp_path):
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import override_settings

    user = actor(platform=True)
    client = Client(enforce_csrf_checks=True)
    client.get("/accounts/login/")
    csrf = client.cookies["csrftoken"].value
    assert (
        client.post(
            "/accounts/login/", {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": csrf}
        ).status_code
        == 302
    )
    csrf = client.cookies["csrftoken"].value
    assert (
        client.post("/platform/updates/upload/", {"package": SimpleUploadedFile("app.skapp", b"unsigned")}).status_code
        == 403
    )
    with override_settings(SKELETON_PACKAGE_MAX_BYTES=8):
        response = client.post(
            "/platform/updates/upload/",
            {
                "csrfmiddlewaretoken": csrf,
                "package": SimpleUploadedFile("app.skapp", b"123456789"),
            },
        )
        assert response.status_code == 413
    assert DeploymentOperation.objects.count() == 0


def test_status_ticket_rotation_does_not_rewrite_durable_state():
    from skeleton.audit.models import AuditEvent
    from skeleton.system.releases import reconcile_operation

    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        row = DeploymentOperation(
            operation="install", expected_generation=0, plan_digest="d" * 64, plan={"generation": 0}
        )
        row.save()
        status = agent_status(row, status_url="https://status.example.test/?ticket=private-first")
        reconcile_operation(row, status)
        revision = row.revision
        event_ids = set(AuditEvent.objects.filter(record_sysid=row.pk).values_list("pk", flat=True))
        status["status_url"] = "https://status.example.test/?ticket=private-second"
        reconcile_operation(row, status)
        persisted = DeploymentOperation.objects.get(pk=row.pk)
        assert persisted.revision == revision
        assert "status_url" not in persisted.result
        assert set(AuditEvent.objects.filter(record_sysid=row.pk).values_list("pk", flat=True)) == event_ids


def test_failed_operation_refresh_shows_offline_recovery(monkeypatch):
    from skeleton.web.platform import updates as release_admin

    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        row = DeploymentOperation(
            operation="update",
            state="failed",
            stage="failed",
            expected_generation=0,
            plan_digest="e" * 64,
            plan={"generation": 0},
            error_code="migration_failed",
        )
        row.save()
    status = agent_status(
        row,
        state="restored",
        stage="restored",
        error=None,
        status_url="https://status.example.test/?ticket=private-recovery",
    )
    monkeypatch.setattr(release_admin, "agent_client", lambda: SimpleNamespace(operation=lambda operation_id: status))
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
    assert client.get(f"/platform/updates/operations/{row.pk}/").status_code == 200
    row.refresh_from_db()
    assert row.state == "restored"
    assert row.stage == "restored"
    assert row.error_code == ""
    assert "status_url" not in row.result


@pytest.mark.parametrize("accepted", [False, True])
def test_lost_submission_ack_retries_original_approval_without_new_execution(monkeypatch, accepted):
    from skeleton.audit.models import AuditEvent
    from skeleton.system import releases
    from skeleton.system.deployment_client import AgentError

    user = actor(platform=True)
    approval_id, retry_id = uuid4(), uuid4()
    attempts, bindings, executions = [], {}, []
    with request_context(actor=user, request_id=approval_id, source="test:deployment"):
        row = DeploymentOperation(
            operation="install", expected_generation=0, plan_digest="f" * 64, plan={"id": str(uuid4()), "generation": 0}
        )
        row.save()

    def submit(**values):
        attempts.append(deepcopy(values))
        if len(attempts) == 1 and not accepted:
            raise AgentError("agent_unavailable")
        binding = {key: value for key, value in values.items() if key != "operation_id"}
        existing = bindings.get(values["operation_id"])
        if existing is not None and existing != binding:
            raise AgentError("idempotency_conflict", 409)
        if existing is None:
            bindings[values["operation_id"]] = binding
            executions.append(values["operation_id"])
        if len(attempts) == 1:
            raise AgentError("agent_unavailable")  # Accepted, but the ACK never arrived.
        return agent_status(row, state="succeeded", stage="ready", generation=1)

    client = SimpleNamespace(inventory=lambda: {"generation": 0}, submit=submit)
    monkeypatch.setattr(releases, "agent_client", lambda: client)
    with request_context(actor=user, request_id=approval_id, source="test:deployment"):
        releases.confirm_operation(user=user, operation_id=row.pk, revision=row.revision)
        assert attempts == []  # Submission must wait for COMMIT.
    with request_context(actor=user, request_id=retry_id, source="test:deployment"):
        row.refresh_from_db()
        assert row.state == "submission_failed"
        assert row.result["submission"] == attempts[0]
        failed_revision = row.revision
        # A completed operation can already have advanced inventory. Delivery
        # retry must neither ask for it nor create a different approval.
        client.inventory = lambda: pytest.fail("Retry queried live generation.")
        releases.retry_submission(user=user, operation_id=row.pk, revision=failed_revision)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        row.refresh_from_db()
        assert attempts[1] == attempts[0]
        assert attempts[1]["request_id"] == str(approval_id)
        assert len(executions) == 1
        assert row.state == "succeeded"
        assert row.result["generation"] == 1
        assert "submission" not in row.result
        assert row.expected_generation == 0
        retry_events = AuditEvent.objects.filter(record_sysid=row.pk, record_revision__gt=failed_revision)
        assert {event.request_id for event in retry_events} == {retry_id}
        assert all(event.changes.get("result", {}).get("new") in (None, "[REDACTED]") for event in retry_events)
        with pytest.raises(ConcurrentUpdate):
            releases.retry_submission(user=user, operation_id=row.pk, revision=failed_revision)
        assert len(attempts) == 2


def test_submission_retry_preserves_approver_when_another_platform_user_delivers(monkeypatch):
    from skeleton.system import releases
    from skeleton.system.deployment_client import AgentError

    user = actor(platform=True)
    other, _ = bootstrap_identity(
        email="other-release@example.test", organization="other-release", name="Other release", password=PASSWORD
    )
    with system_context(reason="test:platform"):
        other.is_staff = True
        other.is_superuser = True
        other.save(update_fields={"is_staff", "is_superuser"})
    attempts = []
    with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
        row = DeploymentOperation(
            operation="update", expected_generation=4, plan_digest="a" * 64, plan={"id": str(uuid4()), "generation": 4}
        )
        row.save()

    def submit(**values):
        attempts.append(values)
        if len(attempts) == 1:
            raise AgentError("agent_unavailable")
        return agent_status(row, state="queued", stage="approved")

    monkeypatch.setattr(
        releases, "agent_client", lambda: SimpleNamespace(inventory=lambda: {"generation": 4}, submit=submit)
    )
    with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
        releases.confirm_operation(user=user, operation_id=row.pk, revision=row.revision)
    with request_context(actor=other, request_id=uuid4(), source="test:deployment"):
        row.refresh_from_db()
        releases.retry_submission(user=other, operation_id=row.pk, revision=row.revision)
    with request_context(actor=other, request_id=None, source="test:deployment"):
        row.refresh_from_db()
        assert attempts[1] == attempts[0]
        assert attempts[1]["actor_id"] == str(user.pk)
        assert row.updated_by == other.pk
        assert row.state == "queued"


def test_submission_retry_without_durable_binding_fails_closed(monkeypatch):
    from skeleton.system import releases

    user = actor(platform=True)
    monkeypatch.setattr(releases, "agent_client", lambda: pytest.fail("Missing approval was sent."))
    with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
        row = DeploymentOperation(
            operation="install",
            state="submission_failed",
            expected_generation=0,
            plan_digest="a" * 64,
            plan={"id": str(uuid4()), "generation": 0},
        )
        row.save()
        with pytest.raises(releases.DeploymentUnavailable, match="Original approval"):
            releases.retry_submission(user=user, operation_id=row.pk, revision=row.revision)
        persisted = DeploymentOperation.objects.get(pk=row.pk)
        assert persisted.state == "submission_failed"
        assert persisted.revision == row.revision


@pytest.mark.parametrize(
    "changes",
    [
        {"id": str(uuid4())},
        {"state": "preview"},
        {"stage": ""},
        {"generation": True},
        {"expected_generation": True},
        {"expected_generation": 7},
        {"plan_digest": "b" * 64},
        {"action": "uninstall"},
        {"operation": "uninstall"},
        {"plan": {"generation": 99}},
    ],
)
def test_invalid_status_cannot_overwrite_approved_operation(changes):
    from skeleton.system.releases import DeploymentUnavailable, reconcile_operation

    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        row = DeploymentOperation(
            operation="install",
            state="approved",
            expected_generation=0,
            plan_digest="a" * 64,
            plan={"generation": 0},
            result={"submission": {"request_id": str(uuid4())}},
        )
        row.save()
        original_result = deepcopy(row.result)
        revision = row.revision
        with pytest.raises(DeploymentUnavailable):
            reconcile_operation(row, agent_status(row, **changes))
        persisted = DeploymentOperation.objects.get(pk=row.pk)
        assert persisted.revision == revision
        assert persisted.result == original_result
        assert persisted.state == "approved"


def test_stale_release_confirmation_returns_conflict_without_delivery(monkeypatch):
    from skeleton.system import releases

    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        row = DeploymentOperation(
            operation="install", expected_generation=0, plan_digest="a" * 64, plan={"id": str(uuid4()), "generation": 0}
        )
        row.save()
    monkeypatch.setattr(releases, "agent_client", lambda: pytest.fail("Stale approval contacted agent."))
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
    response = client.post(
        f"/platform/updates/operations/{row.pk}/",
        {
            "revision": row.revision + 1,
            "plan_digest": row.plan_digest,
        },
    )
    assert response.status_code == 409
    with request_context(actor=user, request_id=None, source="test:deployment"):
        row.refresh_from_db()
        assert row.state == "preview"


def test_invalid_journal_rolls_back_inventory_and_preserves_operation(tmp_path):
    import json

    from django.core.management import call_command
    from django.core.management.base import CommandError

    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        row = DeploymentOperation(
            operation="install", state="approved", expected_generation=0, plan_digest="a" * 64, plan={"generation": 0}
        )
        row.save()
    inventory_path, journal_path = tmp_path / "inventory.json", tmp_path / "operation.json"
    inventory_path.write_text(
        json.dumps(
            {
                "generation": 1,
                "apps": [
                    {
                        "app_id": "pending-component",
                        "app_label": "pending_component",
                        "package_name": "pending_component",
                        "version": "1.0.0",
                        "state": "installed",
                    }
                ],
            }
        )
    )
    journal_path.write_text(json.dumps(agent_status(row, state="preview", stage="preview")))
    with pytest.raises(CommandError, match="DeploymentUnavailable"):
        call_command("sync_deployment_state", inventory=inventory_path, operation=journal_path)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        persisted = DeploymentOperation.objects.get(pk=row.pk)
        assert persisted.state == "approved"
        assert persisted.revision == row.revision
        assert not ComponentInstallation.objects.filter(component_id="pending-component").exists()


def test_release_services_reject_actor_different_from_context(monkeypatch):
    """A platform administrator passed as ``user`` while another identity is the actor is refused (#285)."""
    from django.core.exceptions import PermissionDenied

    from skeleton.system import releases

    user = actor(platform=True)
    other, _ = bootstrap_identity(
        email="other-guard@example.test", organization="other-guard", name="Other guard", password=PASSWORD
    )
    with system_context(reason="test:platform"):
        other.is_staff = True
        other.is_superuser = True
        other.save(update_fields={"is_staff", "is_superuser"})
    with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
        row = DeploymentOperation(
            operation="update", expected_generation=4, plan_digest="a" * 64, plan={"id": str(uuid4()), "generation": 4}
        )
        row.save()
    monkeypatch.setattr(releases, "agent_client", lambda: pytest.fail("A refused call contacted the agent."))
    with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
        with pytest.raises(PermissionDenied):
            releases.confirm_operation(user=other, operation_id=row.pk, revision=row.revision)
        assert DeploymentOperation.objects.get(pk=row.pk).state == "preview"


# --------------------------------------------------------------------------- stalled deliveries (#250)


def _stalled(monkeypatch):
    """An operation approved whose delivery callback never reported back (approved/awaiting_agent)."""
    from skeleton.system import releases

    user = actor(platform=True)
    with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
        row = DeploymentOperation(
            operation="update", expected_generation=4, plan_digest="b" * 64, plan={"id": str(uuid4()), "generation": 4}
        )
        row.save()
    monkeypatch.setattr(releases, "agent_client", lambda: SimpleNamespace(inventory=lambda: {"generation": 4}))
    with monkeypatch.context() as quiet:
        quiet.setattr(releases, "_schedule_submission", lambda **kwargs: None)  # The delivery never runs.
        with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
            releases.confirm_operation(user=user, operation_id=row.pk, revision=row.revision)
            row.refresh_from_db()
    assert (row.state, row.stage) == ("approved", "awaiting_agent")
    return user, row


def test_stale_approved_operation_can_be_resent(monkeypatch):
    from datetime import timedelta

    from skeleton.system import releases

    user, row = _stalled(monkeypatch)
    assert releases.submission_can_retry(row, now=row.updated_at + timedelta(minutes=3)) is True
    sent = []
    monkeypatch.setattr(
        releases,
        "agent_client",
        lambda: SimpleNamespace(
            submit=lambda **values: sent.append(values) or agent_status(row, state="queued", stage="approved")
        ),
    )
    monkeypatch.setattr(releases, "SUBMISSION_STALE_AFTER", timedelta(0))
    with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
        releases.retry_submission(user=user, operation_id=row.pk, revision=row.revision)
    assert sent == [row.result["submission"]]


def test_fresh_approved_operation_cannot_be_resent(monkeypatch):
    from skeleton.system import releases

    user, row = _stalled(monkeypatch)
    monkeypatch.setattr(releases, "agent_client", lambda: pytest.fail("A fresh approval contacted the agent."))
    with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
        with pytest.raises(ConcurrentUpdate):
            releases.retry_submission(user=user, operation_id=row.pk, revision=row.revision)


def _page(monkeypatch, user, row):
    from skeleton.system.deployment_client import AgentError
    from skeleton.web.platform import updates as release_admin

    def missing(operation_id):
        raise AgentError("not_found", 404)

    monkeypatch.setattr(release_admin, "agent_client", lambda: SimpleNamespace(operation=missing))
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
    return client.get(f"/platform/updates/operations/{row.pk}/")


def test_operation_page_offers_retry_only_when_stale(monkeypatch):
    from datetime import timedelta

    from skeleton.system import releases

    user, row = _stalled(monkeypatch)
    assert _page(monkeypatch, user, row).context["can_retry"] is False
    monkeypatch.setattr(releases, "SUBMISSION_STALE_AFTER", timedelta(0))
    page = _page(monkeypatch, user, row)
    assert page.context["can_retry"] is True and 'name="intent" value="retry"' in page.content.decode()


def test_operation_page_queries_agent_without_row_lock(monkeypatch):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from skeleton.web.platform import updates as release_admin

    user, row = _stalled(monkeypatch)
    locked_before_agent = []

    def operation(operation_id):
        locked_before_agent.append(
            any(
                "FOR UPDATE" in query["sql"] and DeploymentOperation._meta.db_table in query["sql"]
                for query in captured
            )
        )
        return agent_status(row, state="running", stage="backup_ready")

    monkeypatch.setattr(release_admin, "agent_client", lambda: SimpleNamespace(operation=operation))
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
    with CaptureQueriesContext(connection) as captured:
        assert client.get(f"/platform/updates/operations/{row.pk}/").status_code == 200
    assert locked_before_agent == [False]


def test_submission_failure_is_logged(monkeypatch, caplog):
    import logging

    from skeleton.system import releases
    from skeleton.system.deployment_client import AgentError

    user = actor(platform=True)
    with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
        row = DeploymentOperation(
            operation="update",
            expected_generation=4,
            plan_digest="c" * 64,
            plan={"id": str(uuid4()), "generation": 4, "secret_plan": "never-logged"},
        )
        row.save()

    def submit(**values):
        raise AgentError("agent_unavailable")

    monkeypatch.setattr(
        releases, "agent_client", lambda: SimpleNamespace(inventory=lambda: {"generation": 4}, submit=submit)
    )
    with caplog.at_level(logging.INFO, logger="skeleton.security"):
        with request_context(actor=user, request_id=uuid4(), source="test:deployment"):
            releases.confirm_operation(user=user, operation_id=row.pk, revision=row.revision)
    [record] = [item for item in caplog.records if getattr(item, "event", "") == "deployment.submission_failed"]
    assert record.target_sysid == str(row.pk) and record.error_class == "AgentError"
    assert "never-logged" not in caplog.text and row.plan_digest not in caplog.text


def test_sync_deployment_state_skips_organizations_without_retained_apps(tmp_path, monkeypatch):
    """Without a retained app no organization context is opened (#284)."""
    import json

    from django.core.management import call_command

    from skeleton.system.management.commands import sync_deployment_state as command

    actor(platform=True)  # At least one organization exists.
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps({"generation": 0, "apps": []}))
    real, reasons = command.system_context, []

    def spy(*args, **kwargs):
        reasons.append(kwargs.get("reason"))
        return real(*args, **kwargs)

    monkeypatch.setattr(command, "system_context", spy)
    call_command("sync_deployment_state", inventory=inventory_path)
    assert "deployment:deactivate" not in reasons and "deployment:reconcile" in reasons


# Asynchronous preview (#298): the plan is prepared by the agent; the web follows it read-only.
PLAN_ID = "4b0a3c5e-9d1f-4c2a-8e7b-1f2a3b4c5d6e"


def ready_plan(action="install"):
    plan = {"id": PLAN_ID, "action": action, "generation": 3, "components": [], "files": [], "migrations": []}
    plan["digest"] = "e" * 64
    return plan


def preview_agent(monkeypatch, *states, calls=None):
    """An agent double whose plan(...) answers the given states in order (the last one repeats)."""
    from skeleton.system import releases

    answers = list(states)
    calls = [] if calls is None else calls

    def preview(action, artifacts=None, app_ids=None):
        calls.append(("preview", action, artifacts, app_ids))
        return {"id": PLAN_ID, "state": "preparing"}

    def plan(plan_id):
        calls.append(("plan", plan_id))
        state = answers.pop(0) if len(answers) > 1 else answers[0]
        status = {"id": PLAN_ID, "state": state, "created_at": "2026-11-13T00:00:00+00:00"}
        status["request"] = {"action": "install", "artifacts": ["c" * 64], "app_ids": []}
        if state == "ready":
            status["plan"] = ready_plan()
        if state == "failed":
            status["error"] = "dependency_unsatisfied"
            suggestion = {"source_id": "main", "component_id": "org.test.base", "version": "1.2.0", "digest": "b" * 64}
            status["details"] = [
                {
                    "code": "missing_dependency",
                    "component": "org.test.consumer",
                    "dependency": "org.test.base",
                    "required": ">=1.0",
                    "suggestions": [suggestion],
                }
            ]
        return status

    monkeypatch.setattr(releases, "agent_client", lambda: SimpleNamespace(preview=preview, plan=plan))
    return calls


def platform_login():
    user = actor(platform=True)
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
    return user, client


def audit_count():
    from skeleton.audit.models import AuditEvent

    with system_context(reason="test:audit-count"):
        return AuditEvent.objects.count()


def test_plan_form_redirects_to_preview_page_without_creating_operation(monkeypatch):
    calls = preview_agent(monkeypatch, "preparing")
    _, client = platform_login()
    response = client.post("/platform/updates/plan/", {"action": "install", "artifacts": "c" * 64})
    assert response.status_code == 302
    assert response["Location"] == f"/platform/updates/plan/{PLAN_ID}/"
    assert calls == [("preview", "install", ["c" * 64], [])]
    assert not DeploymentOperation.objects.exists()


def test_preview_page_get_is_read_only_while_preparing(monkeypatch):
    preview_agent(monkeypatch, "preparing")
    _, client = platform_login()
    before = audit_count()
    response = client.get(f"/platform/updates/plan/{PLAN_ID}/")
    assert response.status_code == 200
    page = response.content.decode()
    assert '<meta http-equiv="refresh" content="3">' in page and "Preparando a prévia" in page
    assert not DeploymentOperation.objects.exists() and audit_count() == before


def test_preview_page_shows_ready_plan_without_writing(monkeypatch):
    preview_agent(monkeypatch, "ready")
    _, client = platform_login()
    before = audit_count()
    page = client.get(f"/platform/updates/plan/{PLAN_ID}/").content.decode()
    assert "http-equiv" not in page and "Continuar" in page and "e" * 64 in page
    assert not DeploymentOperation.objects.exists() and audit_count() == before


def test_preview_page_shows_failure_details(monkeypatch):
    preview_agent(monkeypatch, "failed")
    _, client = platform_login()
    response = client.get(f"/platform/updates/plan/{PLAN_ID}/")
    assert response.status_code == 409
    page = response.content.decode()
    assert "org.test.consumer exige org.test.base &gt;=1.0" in page and "Adicionar ao plano" in page
    assert f'name="artifacts" value="{"c" * 64}"' in page
    assert not DeploymentOperation.objects.exists()


def test_continue_creates_single_operation_and_redirects(monkeypatch):
    preview_agent(monkeypatch, "ready")
    _, client = platform_login()
    first = client.post(f"/platform/updates/plan/{PLAN_ID}/")
    second = client.post(f"/platform/updates/plan/{PLAN_ID}/")
    row = DeploymentOperation.objects.get()
    assert first["Location"] == second["Location"] == f"/platform/updates/operations/{row.pk}/"
    assert (row.operation, row.expected_generation, row.plan_digest, row.state) == ("install", 3, "e" * 64, "preview")
    assert row.plan == ready_plan()


def test_continue_refuses_plan_that_is_not_ready(monkeypatch):
    preview_agent(monkeypatch, "preparing")
    _, client = platform_login()
    response = client.post(f"/platform/updates/plan/{PLAN_ID}/")
    assert response.status_code == 302 and response["Location"] == f"/platform/updates/plan/{PLAN_ID}/"
    assert not DeploymentOperation.objects.exists()


def test_preview_page_requires_platform_administrator(monkeypatch):
    from skeleton.system import releases

    monkeypatch.setattr(releases, "agent_client", lambda: pytest.fail("A refused page contacted the agent."))
    user = actor()
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
    assert client.get(f"/platform/updates/plan/{PLAN_ID}/").status_code == 403
    assert client.post(f"/platform/updates/plan/{PLAN_ID}/").status_code == 403
    assert not DeploymentOperation.objects.exists()


def test_old_agent_answering_with_whole_plan_is_refused(monkeypatch):
    from skeleton.system import releases
    from skeleton.system.releases import DeploymentUnavailable, request_preview

    monkeypatch.setattr(releases, "agent_client", lambda: SimpleNamespace(preview=lambda *a, **k: ready_plan()))
    user = actor(platform=True)
    with request_context(actor=user, request_id=None, source="test:deployment"):
        with pytest.raises(DeploymentUnavailable, match="older than this core"):
            request_preview(user=user, action="install", artifacts=["c" * 64])
    assert not DeploymentOperation.objects.exists()
