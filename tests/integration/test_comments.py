"""Comments on records (#187), PostgreSQL with the runtime role: the new-table matrix.

Authorization derives from the target (404 without read, A/B), the table must opt in, only the
author edits, author and admin delete, restore keeps the UUID, CAS has one winner, every write
is audited, bodies are escaped, comments appear on the timeline and CommentService passes the
service contract.
"""

from dataclasses import replace

import pytest
from django.test import Client

from skeleton.service_api.comments import CommentService
from skeleton.service_api.errors import Conflict, Forbidden, InvalidInput, NotFound
from skeleton.system import registry
from skeleton.system.registry import CommentPolicy
from skeleton.testing.service_api import (
    FIXTURE_PASSWORD,
    acting_as,
    assert_cas_conflict,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    build_service_scenario,
)

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"


def test_contract():
    assert_service_contract(CommentService)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setitem(registry._registry, TABLE, replace(registry._registry[TABLE], comments=CommentPolicy()))


@pytest.fixture
def scenario(enabled):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="comments")
    scenario.note = scenario.create(scenario.a, code="C-1", title="Comentável")
    scenario.private = scenario.create(
        scenario.a, owner=scenario.admin_a, code="C-2", title="Privada", visibility="private"
    )
    scenario.foreign = scenario.create(scenario.b, owner=scenario.admin_b, code="C-3", title="De B")
    return scenario


def _login(user):
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


def _url(organization, note, suffix=""):
    return f"/api/v1/o/{organization.pk}/tables/{TABLE}/records/{note.pk}/comments/{suffix}"


def _add(scenario, user, body="Primeiro", note=None):
    with acting_as(user, scenario.a) as actor:
        return CommentService().add_comment(
            actor=actor, table_key=TABLE, record_id=(note or scenario.note).pk, body=body
        )


def test_readers_comment_and_unreadable_targets_answer_404(scenario, subtests):
    member = _login(scenario.member)
    with subtests.test("create and list"):
        created = member.post(
            _url(scenario.a, scenario.note), {"body": "Olá <b>time</b>"}, content_type="application/json"
        )
        assert created.status_code == 201, created.content
        assert created["ETag"] == '"1"' and created.json()["author_id"] == str(scenario.member.pk)
        listed = member.get(_url(scenario.a, scenario.note)).json()["results"]
        assert [row["body"] for row in listed] == ["Olá <b>time</b>"]
    with subtests.test("unreadable record"):
        assert member.get(_url(scenario.a, scenario.private)).status_code == 404
        assert (
            member.post(_url(scenario.a, scenario.private), {"body": "x"}, content_type="application/json").status_code
            == 404
        )
    with subtests.test("default deny"):
        denied = _login(scenario.denied)
        assert denied.get(_url(scenario.a, scenario.note)).status_code == 404
        assert (
            denied.post(_url(scenario.a, scenario.note), {"body": "x"}, content_type="application/json").status_code
            == 404
        )
    with subtests.test("A/B"):
        assert member.get(_url(scenario.a, scenario.foreign)).status_code == 404
        assert member.get(_url(scenario.b, scenario.note)).status_code == 404
        assert_tenant_isolated(
            scenario.caller(
                lambda actor: CommentService().comments(actor=actor, table_key=TABLE, record_id=scenario.note.pk),
                organization=scenario.a,
            ),
            foreign=[scenario.foreign],
        )
    with subtests.test("invalid bodies"):
        for body in ("", "x" * 5001, 3):
            assert (
                member.post(
                    _url(scenario.a, scenario.note), {"body": body}, content_type="application/json"
                ).status_code
                == 400
            )
        assert (
            member.post(
                _url(scenario.a, scenario.note), {"body": "x", "author": "y"}, content_type="application/json"
            ).status_code
            == 400
        )


def test_table_without_policy_refuses_comments(scenario, monkeypatch):
    monkeypatch.setitem(registry._registry, TABLE, replace(registry._registry[TABLE], comments=None))
    member = _login(scenario.member)
    assert member.get(_url(scenario.a, scenario.note)).status_code == 404
    assert (
        member.post(_url(scenario.a, scenario.note), {"body": "x"}, content_type="application/json").status_code == 404
    )
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(NotFound):
        CommentService().add_comment(actor=actor, table_key=TABLE, record_id=scenario.note.pk, body="x")


def test_author_edits_and_author_or_admin_delete_and_restore(scenario, subtests):
    comment = _add(scenario, scenario.member)
    member, admin = _login(scenario.member), _login(scenario.admin_a)
    detail = _url(scenario.a, scenario.note, f"{comment.sysid}/")
    with subtests.test("only the author edits"):
        refused = admin.patch(detail, {"body": "do admin"}, content_type="application/json", HTTP_IF_MATCH='"1"')
        assert refused.status_code == 403
        edited = member.patch(detail, {"body": "Editado"}, content_type="application/json", HTTP_IF_MATCH='"1"')
        assert edited.status_code == 200 and edited.json()["edited_at"] and edited["ETag"] == '"2"'
    with subtests.test("CAS"):
        stale = member.patch(detail, {"body": "velho"}, content_type="application/json", HTTP_IF_MATCH='"1"')
        assert stale.status_code in (409, 412)
        assert member.patch(detail, {"body": "sem revisão"}, content_type="application/json").status_code == 428
    with subtests.test("another member cannot delete"):
        other = _add(scenario, scenario.admin_a, "Do admin")
        url = _url(scenario.a, scenario.note, f"{other.sysid}/")
        assert member.delete(url, HTTP_IF_MATCH='"1"').status_code == 403
    with subtests.test("admin moderates and restores"):
        assert admin.delete(detail, HTTP_IF_MATCH='"2"').status_code == 204
        assert [row["sysid"] for row in member.get(_url(scenario.a, scenario.note)).json()["results"]] == [
            str(other.sysid)
        ]
        restored = admin.post(
            _url(scenario.a, scenario.note, f"{comment.sysid}/restore/"),
            HTTP_IF_MATCH='"3"',
            content_type="application/json",
        )
        assert (
            restored.status_code == 200
            and restored.json()["sysid"] == str(comment.sysid)
            and restored.json()["body"] == "Editado"
        )
    with subtests.test("author deletes own"):
        assert member.delete(detail, HTTP_IF_MATCH='"4"').status_code == 204
        assert (
            _login(scenario.member)
            .post(
                _url(scenario.a, scenario.note, f"{comment.sysid}/restore/"),
                HTTP_IF_MATCH='"5"',
                content_type="application/json",
            )
            .status_code
            == 200
        )


def test_service_errors_cas_and_actor(scenario, subtests):
    service = CommentService()
    comment = _add(scenario, scenario.member)
    with subtests.test("CAS one winner"):

        def edit(body):
            def call():
                with acting_as(scenario.member, scenario.a) as actor:
                    return service.edit_comment(
                        actor=actor,
                        table_key=TABLE,
                        record_id=scenario.note.pk,
                        comment_id=comment.sysid,
                        body=body,
                        expected_revision=1,
                    )

            return call

        assert_cas_conflict(edit("A"), edit("B"))
    with acting_as(scenario.admin_a, scenario.a) as admin:
        with subtests.test("forbidden edit"), pytest.raises(Forbidden):
            service.edit_comment(
                actor=admin,
                table_key=TABLE,
                record_id=scenario.note.pk,
                comment_id=comment.sysid,
                body="x",
                expected_revision=2,
            )
        with subtests.test("stale delete"), pytest.raises(Conflict):
            service.delete_comment(
                actor=admin, table_key=TABLE, record_id=scenario.note.pk, comment_id=comment.sysid, expected_revision=1
            )
    with acting_as(scenario.member, scenario.a) as member:
        with subtests.test("invalid"), pytest.raises(InvalidInput):
            service.add_comment(actor=member, table_key=TABLE, record_id=scenario.note.pk, body="")
        with subtests.test("missing"), pytest.raises(NotFound):
            service.restore_comment(
                actor=member, table_key=TABLE, record_id=scenario.note.pk, comment_id=comment.sysid, expected_revision=2
            )
    assert_rejects_other_actor(
        scenario.caller(
            lambda actor: service.comments(actor=scenario.denied, table_key=TABLE, record_id=scenario.note.pk)
        )
    )


def test_writes_are_audited_and_rollback_leaves_nothing(scenario):
    from django.db import transaction

    from skeleton.audit.models import AuditEvent
    from skeleton.comments.models import Comment
    from skeleton.tenancy.context import system_context

    comment = _add(scenario, scenario.member)
    with acting_as(scenario.member, scenario.a) as actor:
        CommentService().edit_comment(
            actor=actor,
            table_key=TABLE,
            record_id=scenario.note.pk,
            comment_id=comment.sysid,
            body="Dois",
            expected_revision=1,
        )
        with pytest.raises(RuntimeError), transaction.atomic():
            CommentService().add_comment(actor=actor, table_key=TABLE, record_id=scenario.note.pk, body="Desfeito")
            raise RuntimeError
    with system_context(reason="test:comment-audit", organization_id=scenario.a.pk):
        events = list(AuditEvent.objects.filter(model_label="sk_comments.comment").order_by("created_at"))
        assert [event.operation for event in events] == ["create", "update"]
        assert events[1].changes["body"]["new"] == "Dois"
        assert not Comment.objects.filter(body="Desfeito").exists()


def test_page_escapes_bodies_and_timeline_shows_comments(scenario):
    _add(scenario, scenario.member, 'Veja https://example.test <script>alert("x")</script>')
    member = _login(scenario.member)
    page = member.get(f"/o/{scenario.a.pk}/tables/{TABLE}/{scenario.note.pk}/")
    assert page.status_code == 200
    assert b"<script>alert" not in page.content and b"&lt;script&gt;alert" in page.content
    assert b'href="https://example.test"' in page.content
    assert b'data-timeline-kind="comment"' in page.content
    posted = member.post(f"/o/{scenario.a.pk}/tables/{TABLE}/{scenario.note.pk}/comments/", {"body": "Pela página"})
    assert posted.status_code == 302
    assert _login(scenario.denied).post(
        f"/o/{scenario.a.pk}/tables/{TABLE}/{scenario.note.pk}/comments/", {"body": "x"}
    ).status_code in (403, 404)
