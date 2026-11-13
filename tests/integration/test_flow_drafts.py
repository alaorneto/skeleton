"""Flows authored as data (#414): draft, validate, publish, start; and every refusal."""

import json

import pytest
from django.urls import reverse

from skeleton.service_api import InvalidInput
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD
from tests import support
from tests.integration.test_flow_service_api import approval_spec, as_app, scenario, service

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]
DATA_KEY = "example_notes.data_approval"


def data_specification(**changes):
    raw = approval_spec().to_json()
    raw.pop("version")
    raw.update({"key": DATA_KEY, "label": "Aprovação em dados", **changes})
    return raw


def client(user):
    return support.login(user, password=FIXTURE_PASSWORD, csrf=False)


def send(http, method, url, body=None, revision=None):
    headers = {"HTTP_IF_MATCH": f'"{revision}"'} if revision is not None else {}
    return getattr(http, method)(url, data=json.dumps(body or {}), content_type="application/json", **headers)


def urls(organization, draft_id=None):
    base = reverse("flows:api-drafts", args=[organization.pk])
    if draft_id is None:
        return base
    return reverse("flows:api-draft", args=[organization.pk, draft_id]), reverse(
        "flows:api-draft-publish", args=[organization.pk, draft_id]
    )


def start_data_flow(scenario, note):
    return scenario.caller(lambda actor: as_app(service.start)(actor=actor, flow_key=DATA_KEY, subject_id=note.pk))()


def test_a_draft_is_validated_published_and_started(scenario):
    admin = client(scenario.admin_a)
    created = send(admin, "post", urls(scenario.a), {"specification": data_specification()})
    assert created.status_code == 201, created.content
    draft = created.json()
    assert draft["problems"] == [] and draft["published_version"] is None and "version" not in draft["specification"]
    detail, publish = urls(scenario.a, draft["sysid"])
    published = send(admin, "post", publish, revision=draft["revision"])
    assert published.status_code == 200, published.content
    assert published.json()["published_version"] == 1
    first = start_data_flow(scenario, scenario.note)
    changed = data_specification(label="Aprovação em dados, v2")
    edited = send(admin, "patch", detail, {"specification": changed}, revision=published.json()["revision"])
    assert edited.status_code == 200, edited.content
    second = send(admin, "post", publish, revision=edited.json()["revision"])
    assert second.json()["published_version"] == 2
    later = start_data_flow(scenario, scenario.other)
    info = lambda receipt: scenario.caller(  # noqa: E731
        lambda actor: service.instance(actor=actor, instance_id=receipt.sysid)
    )()
    assert (info(first).flow_version, info(later).flow_version) == (1, 2)
    assert [item["key"] for item in admin.get(urls(scenario.a)).json()["results"]] == [DATA_KEY]


def test_only_registered_references_publish_and_code_keys_are_reserved(scenario):
    admin = client(scenario.admin_a)
    broken = data_specification()
    broken["steps"].insert(
        2, {**broken["steps"][1], "key": "robot", "kind": "service", "handler": "example_notes.does_not_exist"}
    )
    created = send(admin, "post", urls(scenario.a), {"specification": broken}).json()
    assert created["problems"], created
    _, publish = urls(scenario.a, created["sysid"])
    refused = send(admin, "post", publish, revision=created["revision"])
    assert refused.status_code == 400 and refused.json()["fields"]["specification"]
    clash = approval_spec().to_json()
    assert send(admin, "post", urls(scenario.a), {"specification": clash}).status_code == 400
    with pytest.raises(InvalidInput):  # Never published: the key does not exist for starting.
        start_data_flow(scenario, scenario.note)


def test_members_other_organizations_and_stale_revisions_are_refused(scenario):
    admin = client(scenario.admin_a)
    draft = send(admin, "post", urls(scenario.a), {"specification": data_specification()}).json()
    detail, publish = urls(scenario.a, draft["sysid"])
    member = client(scenario.member)
    assert member.get(urls(scenario.a)).status_code == 403
    assert send(member, "post", publish, revision=draft["revision"]).status_code == 403
    other = client(scenario.admin_b)
    assert other.get(urls(scenario.b, draft["sysid"])[0]).status_code == 404
    assert other.get(urls(scenario.b)).json()["results"] == []
    assert send(admin, "post", publish, revision=draft["revision"] + 5).status_code == 412
    assert send(admin, "post", publish).status_code == 428


def test_a_published_version_never_changes(scenario):
    from django.db import transaction

    from skeleton.common.exceptions import ImmutableRecord
    from skeleton.flows.models import FlowDefinition

    admin = client(scenario.admin_a)
    draft = send(admin, "post", urls(scenario.a), {"specification": data_specification()}).json()
    _, publish = urls(scenario.a, draft["sysid"])
    send(admin, "post", publish, revision=draft["revision"])
    with system_context(reason="test:immutable-definition", organization_id=scenario.a.pk):
        definition = FlowDefinition.objects.get(key=DATA_KEY, version=1)
        definition.label = "Trocado"
        with pytest.raises(ImmutableRecord), transaction.atomic():
            definition.save()


def test_the_editor_creates_saves_and_publishes_a_draft(scenario):
    """#415: the same lifecycle through the pages, with CSRF, CAS and the validation shown."""
    admin = support.login(scenario.admin_a, password=FIXTURE_PASSWORD)
    token = lambda: admin.cookies["csrftoken"].value  # noqa: E731
    page = reverse("flows:drafts", args=[scenario.a.pk])
    assert admin.get(page).status_code == 200
    bad = admin.post(page, {"specification": "{not json", "csrfmiddlewaretoken": token()})
    assert bad.status_code == 400 and "JSON inválido" in bad.content.decode()
    created = admin.post(page, {"specification": json.dumps(data_specification()), "csrfmiddlewaretoken": token()})
    assert created.status_code == 302
    detail = created["Location"]
    screen = admin.get(detail)
    body = screen.content.decode()
    assert screen.status_code == 200 and "fill" in body and "Publicar versão nova" in body
    from skeleton.flows.models import FlowDraft

    with system_context(reason="test:editor", organization_id=scenario.a.pk):
        draft = FlowDraft.objects.get(key=DATA_KEY)
    changed = json.dumps(data_specification(label="Editado na tela"))
    stale = admin.post(
        detail,
        {"specification": changed, "revision": draft.revision + 3, "intent": "save", "csrfmiddlewaretoken": token()},
    )
    assert stale.status_code == 409
    saved = admin.post(
        detail, {"specification": changed, "revision": draft.revision, "intent": "save", "csrfmiddlewaretoken": token()}
    )
    assert saved.status_code == 302
    published = admin.post(
        detail,
        {"specification": changed, "revision": draft.revision + 1, "intent": "publish", "csrfmiddlewaretoken": token()},
    )
    assert published.status_code == 302
    with system_context(reason="test:editor", organization_id=scenario.a.pk):
        draft = FlowDraft.objects.get(key=DATA_KEY)
    assert (draft.label, draft.published_version) == ("Editado na tela", 1)
    member = support.login(scenario.member, password=FIXTURE_PASSWORD)
    assert member.get(page).status_code == 403
    assert member.get(detail).status_code == 403
