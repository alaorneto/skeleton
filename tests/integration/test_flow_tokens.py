"""API tokens on the flow routes with the scopes flows:read and flows:write (#410)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.integrations.tokens.services import issue_personal_token, issue_service_token, revoke_token
from skeleton.tenancy.context import request_context
from tests.integration.test_flow_service_api import my_task, scenario, start

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]
NOTE_READ = "example_notes.note:read"


def personal(user, scopes):
    with request_context(actor=user, request_id=uuid4(), source="test:flow-tokens"):
        return issue_personal_token(
            actor=user, name="CLI", scopes=list(scopes), expires_at=timezone.now() + timedelta(days=5)
        )


def bearer(issued):
    return Client(HTTP_AUTHORIZATION=f"Bearer {issued.text}")


def post(client, url, revision, body=None):
    import json

    return client.post(url, data=json.dumps(body or {}), content_type="application/json", HTTP_IF_MATCH=f'"{revision}"')


def test_read_needs_flows_read_and_commands_need_flows_write(scenario):
    start(scenario)
    task = my_task(scenario)
    tasks = reverse("flows:api-tasks", args=[scenario.a.pk])
    detail = reverse("flows:api-task", args=[scenario.a.pk, task.sysid])
    complete = f"{detail}complete/"
    reader = bearer(personal(scenario.member, ["flows:read", NOTE_READ]))
    listed = reader.get(tasks)
    assert listed.status_code == 200, listed.content
    assert str(task.sysid) in listed.content.decode()
    assert reader.get(detail).status_code == 200
    assert post(reader, complete, task.revision, {"data": {"summary": "x"}}).status_code == 403
    without = bearer(personal(scenario.member, [NOTE_READ]))
    assert without.get(tasks).status_code == 403
    writer = bearer(personal(scenario.member, ["flows:read", "flows:write", NOTE_READ]))
    done = post(writer, complete, task.revision, {"data": {"summary": "Resumo"}})
    assert done.status_code == 200, done.content


def test_tokens_never_administer_and_revoked_tokens_are_refused(scenario):
    receipt = start(scenario)
    issued = personal(scenario.member, ["flows:read", "flows:write", NOTE_READ])
    client = bearer(issued)
    instance = reverse("flows:api-instance", args=[scenario.a.pk, receipt.sysid])
    assert client.get(instance).status_code == 200
    suspend = f"{instance}suspend/"
    assert post(client, suspend, receipt.revision).status_code in (403, 404)
    assert client.get(reverse("flows:api-operations", args=[scenario.a.pk])).status_code in (401, 403)
    with request_context(actor=scenario.member, request_id=uuid4(), source="test:flow-tokens"):
        revoke_token(actor=scenario.member, token_id=issued.token.pk, expected_revision=issued.token.revision)
    assert client.get(instance).status_code == 401


def test_a_service_account_token_acts_only_in_its_organization(scenario):
    from skeleton.accounts.services import create_service_account

    with scenario.acting(scenario.admin_a) as admin:
        robot = create_service_account(actor=admin, name="Robô de fluxos")
        issued = issue_service_token(
            actor=admin, account=robot, name="ERP", scopes=["flows:read"], expires_at=timezone.now() + timedelta(days=5)
        )
    client = bearer(issued)
    assert client.get(reverse("flows:api-tasks", args=[scenario.a.pk])).status_code == 200
    assert client.get(reverse("flows:api-tasks", args=[scenario.b.pk])).status_code in (403, 404)
