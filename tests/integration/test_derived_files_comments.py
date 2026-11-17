"""Attachments and comments on rows of a derived table, authorized through the anchor (#563).

Runs on PostgreSQL with the runtime role; the private storage lives under the test's temporary
directory (the ``skeleton_db`` fixture). ``WorkItem`` opts in to attachments and comments;
``WorkItemLine`` opts in to neither.
"""

import io
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from skeleton.comments.mentions import token
from skeleton.files.purge import purge_deleted
from skeleton.service_api.comments import CommentService
from skeleton.service_api.errors import Conflict, Forbidden, InvalidInput, NotFound
from skeleton.service_api.files import FileService
from skeleton.system.registry import AttachmentPolicy, CommentPolicy
from skeleton.tenancy.context import system_context
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario
from tests import support
from tests.integration.derived_support import child, derived_tables, grant, reader_only

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
ITEM = "example_notes.workitem"
LINE = "example_notes.workitemline"
PDF = b"%PDF-1.7\n" + b"d" * 64


@pytest.fixture
def world(owner_schema_editor):
    policies = {
        "attachments": AttachmentPolicy(max_bytes=1000, types=("application/pdf",)),
        "comments": CommentPolicy(),
    }
    with derived_tables(owner_schema_editor, item_spec=policies, sealable=True) as tables:
        scenario = build_service_scenario(tables.workspace, slug="derived-files")
        admin = scenario.admin_a
        spaces = {
            "writable": scenario.create(scenario.a, owner=admin, visibility="private", title="writable"),
            "readable": scenario.create(scenario.a, owner=admin, visibility="organization", title="readable"),
            "hidden": scenario.create(scenario.a, owner=admin, visibility="private", title="hidden"),
        }
        grant(scenario, spaces["writable"], user=scenario.member, action="read")
        grant(scenario, spaces["writable"], user=scenario.member, action="update")
        items = {name: child(tables.item, scenario.a, workspace=space, title=name) for name, space in spaces.items()}
        foreign_space = scenario.create(scenario.b, owner=scenario.admin_b, visibility="organization", title="b")
        items["foreign"] = child(tables.item, scenario.b, workspace=foreign_space, title="foreign")
        line = child(tables.line, scenario.a, item=items["writable"], text="line")
        yield tables, scenario, spaces, items, line


def attach(actor, record, table=ITEM):
    return FileService().attach(
        actor=actor, table_key=table, record_id=record.pk, filename="doc.pdf", stream=io.BytesIO(PDF)
    )


def test_attachments_follow_the_anchor(world):
    tables, scenario, spaces, items, line = world
    service = FileService()
    with acting_as(scenario.member, scenario.a) as actor:
        info = attach(actor, items["writable"])
        listed = service.list(actor=actor, table_key=ITEM, record_id=items["writable"].pk).items
        assert [row.sysid for row in listed] == [info.sysid] and info.table_key == ITEM
        assert service.download_url(actor=actor, attachment_id=info.sysid).endswith(f"/{info.sysid}/download/")
        assert service.list(actor=actor, table_key=ITEM, record_id=items["readable"].pk).items == ()
        with pytest.raises(Forbidden):  # Reads the anchor, cannot update it.
            attach(actor, items["readable"])
        answers = []
        for name in ("hidden", "foreign"):
            with pytest.raises(NotFound) as denied:
                attach(actor, items[name])
            answers.append((denied.value.code, denied.value.detail))
        assert answers[0] == answers[1]
        with pytest.raises(NotFound):  # A derived table without the opt-in has no attachments.
            attach(actor, line, table=LINE)
    with acting_as(scenario.member, scenario.a) as actor:
        with pytest.raises(Conflict):
            service.remove(actor=actor, attachment_id=info.sysid, expected_revision=9)
        service.remove(actor=actor, attachment_id=info.sysid, expected_revision=1)
        assert service.list(actor=actor, table_key=ITEM, record_id=items["writable"].pk).items == ()
    reader = reader_only(scenario, tables.workspace)
    with acting_as(scenario.admin_a, scenario.a) as admin:
        kept = attach(admin, items["readable"])
    with acting_as(reader, scenario.a) as actor:
        assert [
            row.sysid for row in service.list(actor=actor, table_key=ITEM, record_id=items["readable"].pk).items
        ] == [kept.sysid]
        with pytest.raises(Forbidden):
            service.remove(actor=actor, attachment_id=kept.sysid, expected_revision=1)


def test_the_download_reauthorizes_every_request(world):
    from skeleton.access.models import RecordGrant

    tables, scenario, spaces, items, line = world
    with acting_as(scenario.member, scenario.a) as actor:
        info = attach(actor, items["writable"])
        url = FileService().download_url(actor=actor, attachment_id=info.sysid)
    member = support.login(scenario.member, password=FIXTURE_PASSWORD)
    response = member.get(url)
    assert response.status_code == 200 and b"".join(response.streaming_content) == PDF
    outsider = support.login(scenario.admin_b, password=FIXTURE_PASSWORD)  # Another organization, URL in hand.
    assert outsider.get(url).status_code in (403, 404)
    with system_context(reason="test:derived-revoke", organization_id=scenario.a.pk):
        for row in RecordGrant.objects.filter(record_sysid=spaces["writable"].pk):
            row.delete()
    assert member.get(url).status_code == 404  # The grant on the anchor is gone.
    wrong_table = reverse(
        "files:download",
        kwargs={"organization_id": scenario.a.pk, "table_key": LINE, "sysid": line.pk, "attachment_id": info.sysid},
    )
    assert support.login(scenario.admin_a, password=FIXTURE_PASSWORD).get(wrong_table).status_code == 404


def test_purge_treats_the_attachment_of_a_child_like_any_other(world):
    from skeleton.files.models import Attachment

    tables, scenario, spaces, items, line = world
    with acting_as(scenario.member, scenario.a) as actor:
        info = attach(actor, items["writable"])
    with acting_as(scenario.member, scenario.a) as actor:
        FileService().remove(actor=actor, attachment_id=info.sysid, expected_revision=1)
    with system_context(reason="test:derived-purge", organization_id=scenario.a.pk):
        assert purge_deleted(now=timezone.now() + timedelta(days=200)) == 1
        assert Attachment.objects.with_deleted().get(pk=info.sysid).state == "purged"


def test_a_sealed_child_refuses_new_attachments(world):
    from skeleton.common.sealing import digest
    from skeleton.records.sealing import seal_payload

    tables, scenario, spaces, items, line = world
    item = items["writable"]
    with acting_as(scenario.member, scenario.a) as actor:
        item.apply_seal(actor_id=actor.pk, digest=digest(seal_payload(item)), at=timezone.now())
        item.save(update_fields={"sealed_at", "sealed_by", "seal_digest"})
    with acting_as(scenario.member, scenario.a) as actor, pytest.raises(Conflict):
        attach(actor, item)


def test_comments_and_mentions_follow_the_anchor(world):
    tables, scenario, spaces, items, line = world
    service = CommentService()
    with acting_as(scenario.member, scenario.a) as actor:
        comment = service.add_comment(actor=actor, table_key=ITEM, record_id=items["readable"].pk, body="Olá")
        assert comment.table_key == ITEM and comment.record_id == items["readable"].pk
        rows = service.comments(actor=actor, table_key=ITEM, record_id=items["readable"].pk).items
        assert [row.sysid for row in rows] == [comment.sysid]
        for name in ("hidden", "foreign"):
            with pytest.raises(NotFound):
                service.add_comment(actor=actor, table_key=ITEM, record_id=items[name].pk, body="x")
        with pytest.raises(NotFound):
            service.add_comment(actor=actor, table_key=LINE, record_id=line.pk, body="x")
        # The denied member does not read the anchor: the mention is refused, nobody is notified.
        with pytest.raises(InvalidInput):
            service.add_comment(
                actor=actor,
                table_key=ITEM,
                record_id=items["readable"].pk,
                body=f"Veja {token('Denied', scenario.denied.pk)}",
            )
        service.add_comment(
            actor=actor,
            table_key=ITEM,
            record_id=items["readable"].pk,
            body=f"Veja {token('Admin', scenario.admin_a.pk)}",
        )
    with acting_as(scenario.denied, scenario.a) as actor, pytest.raises(NotFound):
        service.comments(actor=actor, table_key=ITEM, record_id=items["readable"].pk)
    from skeleton.notifications.models import Notification

    with system_context(reason="test:derived-mention", organization_id=scenario.a.pk):
        assert Notification.objects.filter(type_code="comments.mention").count() == 1
