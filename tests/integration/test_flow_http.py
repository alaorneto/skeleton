"""REST and pages of the process flows (#206), with a real session login on PostgreSQL."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.access.models import MemberRole, Role
from skeleton.flows import registry as flows
from skeleton.flows import services
from skeleton.flows.models import FlowInstance, FlowTask
from skeleton.tenancy.context import request_context, system_context
from skeleton.testing.service_api import _set_enabled
from tests import support
from tools.security_fixtures import actor_tenant, build_security_fixture

PASSWORD = "Fixture-only-password-2026!"
SECRET = "segredo do formulario 77"
FORM = flows.Form(
    flows.Field("summary", "str", label="Resumo", required=True, max_length=50),
    flows.Field("amount", "decimal", label="Valor"),
    flows.Field("remark", "text", label="Reservado", sensitive=True),
)


@pytest.fixture
def world(skeleton_db, monkeypatch):
    from skeleton.flows import registry

    for name in ("_flows", "_handlers", "_conditions", "_assignments"):
        monkeypatch.setattr(registry, name, {})
    monkeypatch.setattr(registry, "_duplicates", [])
    flows.register_flow(
        flows.FlowSpec(
            key="example_notes.http",
            version=1,
            label="Fluxo HTTP",
            owner_app="example_notes",
            subject_table="example_notes.note",
            steps=(
                flows.start("start"),
                flows.task("fill", label="Preencher", assignees=(flows.Assignment.starter(),), form=FORM),
                flows.approval("review", label="Revisar", assignees=(flows.Assignment.role("note_reviewer"),)),
                flows.end("done", outcome="approved"),
            ),
            transitions=(
                flows.Transition("start", "fill"),
                flows.Transition("fill", "review", key="submit", label="Enviar"),
                flows.Transition("review", "done", on="approved"),
                flows.Transition("review", "fill", on="rejected"),
            ),
        )
    )
    fixture = build_security_fixture()
    with system_context(reason="test:flow-http", organization_id=fixture.a.pk):
        reviewer = Role(organization=fixture.a, code="note_reviewer", name="Revisor")
        reviewer.save()
        MemberRole(organization=fixture.a, membership=fixture.bob_membership, role=reviewer).save()
    with actor_tenant(fixture.alice, fixture.a):
        instance, _ = services.start_instance("example_notes.http", subject_id=fixture.private.pk, actor=fixture.alice)
        fixture.instance_id = instance.pk
        fixture.task_id = FlowTask.objects.get(instance=instance).pk
    return fixture


def login(user):
    return support.login(user, password=PASSWORD)


def api(client, method, url, data=None, revision=None, csrf=True):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value} if csrf else {}
    if revision is not None:
        headers["HTTP_IF_MATCH"] = f'"{revision}"'
    return getattr(client, method)(url, data=json.dumps(data or {}), content_type="application/json", **headers)


def url(name, world, organization=None, **kwargs):
    return reverse(f"flows:{name}", kwargs={"organization_id": (organization or world.a).pk, **kwargs})


def task_action(world, action, task_id=None):
    return url("api-task-action", world, sysid=task_id or world.task_id, action=action)


@pytest.mark.needs_example_app
def test_anonymous_and_people_without_membership_get_nothing(world):
    anonymous = Client()
    assert anonymous.get(url("api-tasks", world)).status_code == 403
    page = anonymous.get(url("tasks", world))
    assert page.status_code == 302 and "/accounts/login/" in page["Location"]
    outsider = login(world.outsider)
    assert outsider.get(url("api-tasks", world)).status_code == 404
    assert outsider.get(url("tasks", world)).status_code == 404
    assert outsider.get(url("api-task", world, sysid=world.task_id)).status_code == 404


@pytest.mark.needs_example_app
def test_task_inbox_detail_and_completion_over_rest(world):
    alice, bob = login(world.alice), login(world.bob)
    mine = alice.get(url("api-tasks", world)).json()
    assert [item["sysid"] for item in mine["results"]] == [str(world.task_id)] and mine["next_cursor"] is None
    assert bob.get(url("api-tasks", world)).json()["results"] == []
    assert bob.get(url("api-task", world, sysid=world.task_id)).status_code == 404  # Not his: as if missing.
    assert bob.get(url("api-task", world, sysid=uuid4())).status_code == 404

    detail = alice.get(url("api-task", world, sysid=world.task_id))
    assert detail.status_code == 200 and detail["ETag"] == '"1"' and detail["X-Request-ID"]
    body = detail.json()
    assert body["can_act"] and [field["name"] for field in body["fields"]] == ["summary", "amount", "remark"]
    assert [action["key"] for action in body["actions"]] == ["submit"]

    complete = task_action(world, "complete")
    payload = {"action": "submit", "data": {"summary": "Pedido", "amount": "12.50", "remark": SECRET}}
    assert api(alice, "post", complete, payload, revision=1, csrf=False).status_code == 403  # CSRF.
    assert api(alice, "post", complete, payload).status_code == 428
    assert api(alice, "post", complete, payload, revision=7).status_code == 412
    bad = api(alice, "post", complete, {"data": {"summary": "", "is_admin": True}}, revision=1)
    assert bad.status_code == 400 and set(bad.json()["fields"]) == {"summary", "is_admin"}
    assert api(alice, "post", complete, {"data": {"summary": "x"}, "owner": "me"}, revision=1).status_code == 400
    assert api(alice, "post", complete, {"data": ["summary"]}, revision=1).status_code == 400
    assert api(bob, "post", complete, payload, revision=1).status_code == 404
    with actor_tenant(world.admin, world.a):
        assert FlowTask.objects.get(pk=world.task_id).status == "open"  # Nothing above wrote anything.

    done = api(alice, "post", complete, payload, revision=1)
    assert done.status_code == 200 and done.json()["status"] == "completed" and done["ETag"] == '"2"'
    assert api(alice, "post", complete, payload, revision=1).status_code == 412  # The second writer is stale.

    queue = bob.get(url("api-tasks", world) + "?scope=available").json()["results"]
    assert len(queue) == 1 and queue[0]["queue"] == "role"
    review = queue[0]["sysid"]
    claimed = api(bob, "post", task_action(world, "claim", review), revision=queue[0]["revision"])
    assert claimed.status_code == 200 and claimed.json()["assignee_id"] == str(world.bob.pk)
    released = api(bob, "post", task_action(world, "release", review), revision=claimed.json()["revision"])
    assert released.status_code == 200 and released.json()["assignee_id"] is None
    assert (
        api(alice, "post", task_action(world, "claim", review), revision=released.json()["revision"]).status_code == 404
    )
    wrong = api(bob, "post", task_action(world, "decide", review), {"decision": "maybe"}, revision=3)
    assert wrong.status_code == 400
    decided = api(
        bob,
        "post",
        task_action(world, "decide", review),
        {"decision": "approved", "comment": "de acordo"},
        revision=released.json()["revision"],
    )
    assert decided.status_code == 200 and decided.json()["decision"] == "approved"

    instance = alice.get(url("api-instance", world, sysid=world.instance_id))
    assert instance.status_code == 200 and instance.json()["status"] == "completed"
    assert instance.json()["data"] == [{"name": "amount", "value": "12.50"}, {"name": "summary", "value": "Pedido"}]
    history = alice.get(url("api-instance-history", world, sysid=world.instance_id) + "?page_size=100")
    kinds = [entry["kind"] for entry in history.json()["results"]]
    assert kinds[0] == "instance_started" and kinds[-1] == "instance_completed"
    assert SECRET not in instance.content.decode() + history.content.decode()
    assert "de acordo" in history.content.decode()
    listed = alice.get(url("api-instances", world) + "?status=completed").json()["results"]
    assert [item["sysid"] for item in listed] == [str(world.instance_id)]
    assert alice.get(url("api-instances", world) + "?order=owner").status_code == 400
    assert alice.get(url("api-tasks", world) + "?scope=everyone").status_code == 400
    assert alice.get(url("api-tasks", world) + "?page_size=abc").status_code == 400


@pytest.mark.needs_example_app
def test_reassign_and_instance_administration_over_rest(world):
    alice, admin, bob = login(world.alice), login(world.admin), login(world.bob)
    moved = api(alice, "post", task_action(world, "reassign"), {"to_user": str(world.bob.pk)}, revision=1)
    assert moved.status_code == 200 and moved.json()["assignee_id"] == str(world.bob.pk)
    nobody = api(admin, "post", task_action(world, "reassign"), {"to_user": str(world.outsider.pk)}, revision=2)
    assert nobody.status_code == 400

    def instance_action(client, action, revision=None, data=None):
        target = url("api-instance-action", world, sysid=world.instance_id, action=action)
        return api(client, "post", target, data, revision=revision)

    current = admin.get(url("api-instance", world, sysid=world.instance_id))
    revision = int(current["ETag"].strip('"'))
    assert instance_action(admin, "suspend").status_code == 428
    assert instance_action(bob, "suspend", revision).status_code == 403  # He takes part, but does not administer.
    assert instance_action(admin, "suspend", revision + 5).status_code == 412
    assert instance_action(admin, "resume", revision, {"reason": "x"}).status_code == 400
    suspended = instance_action(admin, "suspend", revision, {"reason": "orçamento"})
    assert suspended.status_code == 200 and suspended.json()["status"] == "suspended"
    frozen = api(bob, "post", task_action(world, "complete"), {"data": {"summary": "x"}}, revision=2)
    assert frozen.status_code == 409 and frozen.json()["code"] == "instance_suspended"
    revision = int(suspended["ETag"].strip('"'))
    assert instance_action(admin, "retry", revision).status_code == 409
    resumed = instance_action(admin, "resume", revision)
    assert resumed.status_code == 200
    cancelled = instance_action(alice, "cancel", int(resumed["ETag"].strip('"')), {"reason": "desisti"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    unknown = url("api-instance-action", world, sysid=world.instance_id, action="retry").replace("retry", "explode")
    assert api(admin, "post", unknown, revision=1).status_code == 404


@pytest.mark.needs_example_app
def test_other_organization_token_and_disabled_app(world):
    from skeleton.integrations.tokens.services import issue_personal_token

    bob = login(world.bob)  # Administrator of B and member of A.
    for name, kwargs in (
        ("api-task", {"sysid": world.task_id}),
        ("api-instance", {"sysid": world.instance_id}),
        ("api-instance-history", {"sysid": world.instance_id}),
        ("task", {"sysid": world.task_id}),
        ("instance", {"sysid": world.instance_id}),
    ):
        assert bob.get(url(name, world, organization=world.b, **kwargs)).status_code == 404
    assert bob.get(url("api-tasks", world, organization=world.b)).json()["results"] == []
    assert bob.get(url("api-instances", world, organization=world.b)).json()["results"] == []
    cross = api(
        bob,
        "post",
        url("api-task-action", world, organization=world.b, sysid=world.task_id, action="claim"),
        revision=1,
    )
    assert cross.status_code == 404

    with request_context(actor=world.alice, request_id=uuid4(), source="test:flow-token"):
        issued = issue_personal_token(
            actor=world.alice,
            name="CLI",
            scopes=["example_notes.note:read"],
            expires_at=timezone.now() + timedelta(days=1),
        )
    bearer = Client(HTTP_AUTHORIZATION=f"Bearer {issued.text}")
    for target in (url("api-tasks", world), url("api-instance", world, sysid=world.instance_id)):
        refused = bearer.get(target)
        # Since #410 tokens reach the flow routes, but only with flows:read (this one has a table scope only).
        assert refused.status_code == 403 and refused.json()["code"] == "permission_denied"

    alice = login(world.alice)
    with actor_tenant(world.admin, world.a):
        _set_enabled("example_notes", world.admin, enabled=False)
    assert alice.get(url("api-task", world, sysid=world.task_id)).status_code == 404
    assert alice.get(url("api-instance", world, sysid=world.instance_id)).status_code == 404
    assert alice.get(url("task", world, sysid=world.task_id)).status_code == 404
    assert alice.get(url("api-tasks", world)).json()["results"] == []
    assert alice.get(url("api-instances", world)).json()["results"] == []
    done = api(alice, "post", task_action(world, "complete"), {"data": {"summary": "x"}}, revision=1)
    assert done.status_code == 404


def csrf(client):
    return client.cookies["csrftoken"].value


@pytest.mark.needs_example_app
def test_pages_work_without_javascript(world):
    alice, bob, admin = login(world.alice), login(world.bob), login(world.admin)
    inbox = alice.get(url("tasks", world))
    assert inbox.status_code == 200 and "Preencher" in inbox.content.decode() and "Fluxo HTTP" in inbox.content.decode()
    assert "Preencher" not in bob.get(url("tasks", world)).content.decode()
    assert bob.get(url("task", world, sysid=world.task_id)).status_code == 404
    page = alice.get(url("task", world, sysid=world.task_id))
    html = page.content.decode()
    assert page.status_code == 200 and 'name="summary"' in html and 'name="remark"' in html and "Enviar" in html
    target = url("task", world, sysid=world.task_id)
    assert Client(enforce_csrf_checks=True).post(target, {"operation": "submit"}).status_code in (302, 403)
    no_token = alice.post(target, {"operation": "submit", "revision": "1", "summary": "x"})
    assert no_token.status_code == 403
    invalid = alice.post(
        target, {"operation": "submit", "revision": "1", "summary": "", "csrfmiddlewaretoken": csrf(alice)}
    )
    assert invalid.status_code == 400 and "Campo obrigatório" in invalid.content.decode()
    stale = alice.post(
        target, {"operation": "submit", "revision": "9", "summary": "Pedido", "csrfmiddlewaretoken": csrf(alice)}
    )
    assert stale.status_code == 409
    done = alice.post(
        target,
        {
            "operation": "submit",
            "revision": "1",
            "summary": "Pedido",
            "remark": SECRET,
            "comment": "segue",
            "csrfmiddlewaretoken": csrf(alice),
        },
    )
    assert done.status_code == 302 and done["Location"] == url("tasks", world)

    available = bob.get(url("tasks", world) + "?scope=available").content.decode()
    assert "Revisar" in available and "Na fila" in available
    with actor_tenant(world.admin, world.a):
        review = FlowTask.objects.get(instance_id=world.instance_id, status="open")
    target = url("task", world, sysid=review.pk)
    claim = bob.post(target, {"operation": "claim", "revision": review.revision, "csrfmiddlewaretoken": csrf(bob)})
    assert claim.status_code == 302 and claim["Location"] == target
    approval_page = bob.get(target).content.decode()
    assert "Aprovar" in approval_page and "Rejeitar" in approval_page and SECRET not in approval_page
    approved = bob.post(
        target,
        {"operation": "approved", "revision": review.revision + 1, "comment": "ok", "csrfmiddlewaretoken": csrf(bob)},
    )
    assert approved.status_code == 302

    detail = alice.get(url("instance", world, sysid=world.instance_id)).content.decode()
    assert "instance_completed" in detail and "task_decided" in detail and "segue" in detail and SECRET not in detail
    assert "Fluxo HTTP" in alice.get(url("instances", world)).content.decode()
    assert admin.get(url("instance", world, sysid=world.instance_id)).status_code == 200
    with actor_tenant(world.admin, world.a):
        assert FlowInstance.objects.get(pk=world.instance_id).outcome == "approved"


@pytest.mark.needs_example_app
def test_instance_actions_on_the_page_require_post_csrf_and_the_revision(world):
    alice, admin = login(world.alice), login(world.admin)
    suspend = url("instance-action", world, sysid=world.instance_id, action="suspend")
    assert admin.get(suspend).status_code == 405
    assert admin.post(suspend, {"revision": "2"}).status_code == 403  # No CSRF token.
    with actor_tenant(world.admin, world.a):
        revision = FlowInstance.objects.get(pk=world.instance_id).revision
    assert alice.post(suspend, {"revision": revision, "csrfmiddlewaretoken": csrf(alice)}).status_code == 403
    stale = admin.post(suspend, {"revision": revision + 3, "csrfmiddlewaretoken": csrf(admin)}, follow=True)
    assert "mudaram desde" in stale.content.decode()
    ok = admin.post(suspend, {"revision": revision, "csrfmiddlewaretoken": csrf(admin)})
    assert ok.status_code == 302
    with actor_tenant(world.admin, world.a):
        assert FlowInstance.objects.get(pk=world.instance_id).status == "suspended"
    page = admin.get(url("instance", world, sysid=world.instance_id)).content.decode()
    assert "Retomar" in page and "Cancelar processo" in page


def test_every_flow_route_is_in_the_openapi_schema():
    from skeleton.api.openapi import UNDOCUMENTED
    from skeleton.testing.openapi import contract_schema

    paths = contract_schema()["paths"]
    base = "/api/v1/o/{organization_id}/flows/"
    for path in ("tasks/", "tasks/{sysid}/", "instances/", "instances/{sysid}/", "instances/{sysid}/history/"):
        assert not paths[base + path]["get"].get(UNDOCUMENTED), path
    commands = [f"tasks/{{sysid}}/{action}/" for action in ("claim", "release", "reassign", "complete", "decide")]
    commands += [f"instances/{{sysid}}/{action}/" for action in ("cancel", "suspend", "resume", "retry")]
    for path in commands:
        operation = paths[base + path]["post"]
        assert not operation.get(UNDOCUMENTED), path
        assert {"412", "428"} <= set(operation["responses"]), path  # If-Match is required.
        assert operation["security"] == [{"bearerAuth": []}, {"sessionAuth": []}], (
            path
        )  # Token with flows:write (#410).
    detail = paths[base + "tasks/{sysid}/"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert {"task", "fields", "actions", "can_act"} <= set(detail["properties"])
