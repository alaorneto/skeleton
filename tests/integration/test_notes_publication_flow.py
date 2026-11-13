"""The publication flow of the Notes example, end to end (#207): PostgreSQL with the runtime role."""

from uuid import uuid4

import pytest

from examples.notes.models import Note
from examples.notes.service_api import NoteService, PublicationStatus
from skeleton.access.models import Role
from skeleton.access.services import provision_membership
from skeleton.audit.models import AuditEvent
from skeleton.flows import registry as flows
from skeleton.service_api import Conflict, Forbidden
from skeleton.service_api.flows import FlowService
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import (
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
    build_service_scenario,
)

pytestmark = pytest.mark.needs_example_app

SECRET = "observação reservada 9911"
notes, engine = NoteService(), FlowService()


@pytest.fixture
def scenario(skeleton_db):
    built = build_service_scenario(Note, slug="notes-publication")
    with system_context(reason="fixture:note-reviewer", organization_id=built.a.pk):
        reviewer = Role(organization=built.a, code="note_reviewer", name="Revisor de anotações")
        reviewer.save()
        # The reviewer has no table permission: an approver who cannot read the note itself.
        provision_membership(organization=built.a, user=built.denied, role=reviewer)
    built.note = built.create(built.a, code="pub", title="Para publicar")
    built.foreign = built.create(built.b, code="foreign-code", title="Foreign title")
    return built


def call(scenario, function, user=None, organization=None):
    return scenario.caller(function, user=user, organization=organization)()


def request(scenario, note=None, **kwargs):
    note = note or scenario.note
    return call(scenario, lambda actor: notes.request_publication(actor=actor, note_id=note.pk, **kwargs))


def task_of(scenario, user=None, scope="mine"):
    return call(scenario, lambda actor: engine.tasks(actor=actor, scope=scope), user=user).items[0]


def prepare(scenario, audience, action="submit", **data):
    task = task_of(scenario)
    values = (("audience", audience), *data.items())
    return call(
        scenario,
        lambda actor: engine.complete_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, action=action, data=values
        ),
    )


def decide(scenario, user, decision, comment=""):
    task = task_of(scenario, user=user, scope="available")
    return call(
        scenario,
        lambda actor: engine.decide_task(
            actor=actor, task_id=task.sysid, expected_revision=task.revision, decision=decision, comment=comment
        ),
        user=user,
    )


def status(scenario):
    return call(scenario, lambda actor: notes.publication_status(actor=actor, note_id=scenario.note.pk))


def test_the_example_declares_a_valid_flow_and_contract():
    assert flows.flow_problems() == []
    assert flows.get_flow("example_notes.publication").owner_app == "example_notes"
    assert_service_contract(NoteService)


def test_internal_note_needs_one_approval_and_the_service_publishes(scenario):
    assert status(scenario) is None
    started = request(scenario, idempotency_key="pub-1")
    assert isinstance(started, PublicationStatus) and started.created and started.status == "running"
    again = request(scenario, idempotency_key="pub-1")
    assert (again.instance_id, again.created) == (started.instance_id, False)
    with pytest.raises(Conflict) as running:
        request(scenario)
    assert running.value.code == "already_running"

    prepare(scenario, "internal", summary="Nota interna", remark=SECRET)
    decide(scenario, scenario.admin_a, "approved")
    assert status(scenario).status == "running"  # The service step waits for its job.
    run_pending_jobs()
    done = status(scenario)
    assert (done.status, done.outcome, done.instance_id) == ("completed", "published", started.instance_id)
    info = call(scenario, lambda actor: engine.instance(actor=actor, instance_id=started.instance_id))
    data = {item.name: item.value for item in info.data}
    assert data["published_reference"] == f"PUB-{str(scenario.note.pk)[:8]}" and "remark" not in data
    with system_context(reason="test:publication-audit", organization_id=scenario.a.pk):
        assert SECRET not in str(list(AuditEvent.objects.values_list("changes", flat=True)))
    assert request(scenario).created is True  # A finished request frees the note for another one.


def test_public_note_needs_both_parallel_approvals(scenario):
    started = request(scenario)
    prepare(scenario, "public")
    info = call(scenario, lambda actor: engine.instance(actor=actor, instance_id=started.instance_id))
    assert {step.key for step in info.active_steps} == {"content", "compliance"}
    # The reviewer cannot read the note, yet takes part in the process and reads the instance.
    reviewer_view = call(
        scenario, lambda actor: engine.instance(actor=actor, instance_id=started.instance_id), user=scenario.denied
    )
    assert reviewer_view.sysid == started.instance_id
    with pytest.raises(Forbidden):
        call(scenario, lambda actor: notes.note_count(actor=actor), user=scenario.denied)

    decide(scenario, scenario.denied, "approved")
    run_pending_jobs()
    assert status(scenario).status == "running"  # One approval is not enough: the join waits.
    info = call(scenario, lambda actor: engine.instance(actor=actor, instance_id=started.instance_id))
    assert {(step.key, step.status) for step in info.active_steps} == {("compliance", "active"), ("joined", "waiting")}
    decide(scenario, scenario.admin_a, "approved")
    run_pending_jobs()
    assert status(scenario).outcome == "published"


def test_rejection_returns_the_request_to_the_author_who_may_withdraw(scenario):
    started = request(scenario)
    prepare(scenario, "public")
    decide(scenario, scenario.denied, "rejected", comment="faltou contexto")
    decide(scenario, scenario.admin_a, "approved")
    back = task_of(scenario)
    assert back.step_key == "prepare" and status(scenario).status == "running"
    history = call(scenario, lambda actor: engine.history(actor=actor, instance_id=started.instance_id, page_size=100))
    assert "faltou contexto" in [entry.comment for entry in history.items]
    prepare(scenario, "public", action="withdraw", withdrawal_reason="fica para depois")
    assert (status(scenario).status, status(scenario).outcome) == ("completed", "withdrawn")


def test_default_deny_isolation_disabled_app_and_actor(scenario):
    with pytest.raises(Forbidden):  # No table permission: Notes answers as its other methods do.
        call(
            scenario,
            lambda actor: notes.request_publication(actor=actor, note_id=scenario.note.pk),
            user=scenario.denied,
        )
    assert_denied_as_not_found(
        scenario.caller(lambda actor: notes.request_publication(actor=actor, note_id=scenario.foreign.pk)),
        missing=scenario.caller(lambda actor: notes.request_publication(actor=actor, note_id=uuid4())),
    )
    assert_denied_as_not_found(
        scenario.caller(lambda actor: notes.publication_status(actor=actor, note_id=scenario.foreign.pk))
    )
    theirs = call(
        scenario,
        lambda actor: notes.request_publication(actor=actor, note_id=scenario.foreign.pk),
        organization=scenario.b,
    )
    request(scenario)
    markers = [scenario.foreign, theirs.instance_id, "foreign-code", "Foreign title"]
    for read in (
        lambda actor: notes.publication_status(actor=actor, note_id=scenario.note.pk),
        lambda actor: engine.instances(actor=actor),
        lambda actor: engine.tasks(actor=actor),
        lambda actor: engine.instance(actor=actor, instance_id=theirs.instance_id),
    ):
        assert_tenant_isolated(scenario.caller(read), foreign=markers)
    assert call(scenario, lambda actor: engine.open_task_count(actor=actor)) == 1

    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: notes.publication_status(actor=actor, note_id=scenario.note.pk)),
        disabled=scenario.application_disabled,
    )
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: notes.request_publication(actor=actor, note_id=scenario.note.pk)),
        disabled=scenario.application_disabled,
    )
    with scenario.application_disabled():
        assert call(scenario, lambda actor: engine.tasks(actor=actor)).items == ()
    assert_rejects_other_actor(
        scenario.caller(lambda actor: notes.request_publication(actor=scenario.admin_a, note_id=scenario.note.pk))
    )
    assert_rejects_other_actor(
        scenario.caller(lambda actor: notes.publication_status(actor=scenario.admin_a, note_id=scenario.note.pk))
    )


def test_page_and_endpoint_start_the_request(scenario):
    import json

    from django.test import Client
    from django.urls import reverse

    from skeleton.testing.service_api import FIXTURE_PASSWORD

    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    token = client.cookies["csrftoken"].value
    logged = client.post(
        reverse("account_login"),
        {"login": scenario.member.email, "password": FIXTURE_PASSWORD, "csrfmiddlewaretoken": token},
    )
    assert logged.status_code == 302
    token = client.cookies["csrftoken"].value
    home = client.get(reverse("example_notes:home", kwargs={"organization_id": scenario.a.pk}))
    assert home.status_code == 200 and "Pedir publicação" in home.content.decode()
    target = reverse(
        "example_notes:request-publication", kwargs={"organization_id": scenario.a.pk, "note_id": scenario.note.pk}
    )
    assert client.get(target).status_code == 405
    assert client.post(target).status_code == 403  # CSRF.
    started = client.post(target, {"csrfmiddlewaretoken": token})
    assert started.status_code == 302 and "/flows/tasks/" in started["Location"]
    inbox = client.get(started["Location"]).content.decode()
    assert "Preparar o pedido" in inbox and "Publicação de anotação" in inbox

    endpoint = reverse("example_notes_api:publication", kwargs={"organization_id": scenario.a.pk})
    read = client.get(endpoint + f"?note={scenario.note.pk}")
    assert read.status_code == 200 and read.json()["publication"]["status"] == "running"
    repeated = client.post(
        endpoint, json.dumps({"note": str(scenario.note.pk)}), content_type="application/json", HTTP_X_CSRFTOKEN=token
    )
    assert repeated.status_code == 409 and repeated.json()["code"] == "already_running"
    missing = client.post(
        endpoint, json.dumps({"note": str(uuid4())}), content_type="application/json", HTTP_X_CSRFTOKEN=token
    )
    assert missing.status_code == 404
