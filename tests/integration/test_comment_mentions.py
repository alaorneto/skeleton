"""Mentions in comments (#188), PostgreSQL with the runtime role.

Mentioning a reader member notifies them once per comment; mentioning a non-reader or a
non-member is refused; editing does not repeat the notification; access revoked between the
mention and the fan-out stops the delivery; A/B.
"""

from dataclasses import replace

import pytest
from django.test import Client

from skeleton.comments.mentions import token
from skeleton.service_api.comments import CommentService
from skeleton.service_api.errors import InvalidInput
from skeleton.system import registry
from skeleton.system.registry import CommentPolicy
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"


@pytest.fixture
def scenario(monkeypatch):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    monkeypatch.setitem(registry._registry, TABLE, replace(registry._registry[TABLE], comments=CommentPolicy()))
    scenario = build_service_scenario(Note, slug="mentions")
    scenario.note = scenario.create(scenario.a, code="M-1", title="Com menções")
    return scenario


def _received(scenario, user):
    from skeleton.notifications.models import NotificationRecipient
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:mentions", organization_id=scenario.a.pk):
        return list(
            NotificationRecipient.objects.filter(
                user_id=user.pk, notification__type_code="comments.mention"
            ).values_list("notification__payload", flat=True)
        )


def _add(scenario, user, body):
    with acting_as(user, scenario.a) as actor:
        return CommentService().add_comment(actor=actor, table_key=TABLE, record_id=scenario.note.pk, body=body)


def test_mentioned_reader_is_notified_once(scenario, subtests):
    comment = _add(scenario, scenario.admin_a, f"Veja isto {token('Membro', scenario.member.pk)}")
    run_pending_jobs()
    with subtests.test("notified"):
        (payload,) = _received(scenario, scenario.member)
        assert payload == {"comment": str(comment.sysid), "table": TABLE, "record": str(scenario.note.pk)}
    with subtests.test("edit does not repeat"):
        with acting_as(scenario.admin_a, scenario.a) as actor:
            CommentService().edit_comment(
                actor=actor,
                table_key=TABLE,
                record_id=scenario.note.pk,
                comment_id=comment.sysid,
                body=f"Editado {token('Membro', scenario.member.pk)}",
                expected_revision=comment.revision,
            )
        run_pending_jobs()
        assert len(_received(scenario, scenario.member)) == 1
    with subtests.test("self mention notifies nobody"):
        _add(scenario, scenario.admin_a, f"eu {token('Eu', scenario.admin_a.pk)}")
        run_pending_jobs()
        assert _received(scenario, scenario.admin_a) == []
    with subtests.test("the notification opens the record"):
        from skeleton.notifications.models import NotificationRecipient
        from skeleton.tenancy.context import system_context

        with system_context(reason="test:mentions", organization_id=scenario.a.pk):
            row = NotificationRecipient.objects.get(
                user_id=scenario.member.pk, notification__type_code="comments.mention"
            )
        client = Client()
        client.post("/accounts/login/", {"login": scenario.member.email, "password": FIXTURE_PASSWORD})
        opened = client.get(f"/o/{scenario.a.pk}/notifications/{row.pk}/open/")
        assert opened.status_code == 302 and str(scenario.note.pk) in opened["Location"]


def test_mentioning_non_readers_or_non_members_is_refused(scenario, subtests):
    from skeleton.comments.models import Comment
    from skeleton.tenancy.context import system_context

    for name, person in (
        ("member without read", scenario.denied),
        ("not a member", scenario.outsider),
        ("member of B only", scenario.admin_b),
    ):
        with subtests.test(name), pytest.raises(InvalidInput):
            _add(scenario, scenario.member, f"oi {token('X', person.pk)}")
    with subtests.test("REST answers 400 and writes nothing"):
        client = Client()
        client.post("/accounts/login/", {"login": scenario.member.email, "password": FIXTURE_PASSWORD})
        response = client.post(
            f"/api/v1/o/{scenario.a.pk}/tables/{TABLE}/records/{scenario.note.pk}/comments/",
            {"body": f"oi {token('X', scenario.denied.pk)}"},
            content_type="application/json",
        )
        assert response.status_code == 400
        with system_context(reason="test:mentions", organization_id=scenario.a.pk):
            assert not Comment.objects.exists()
    with subtests.test("edit adding a non-reader is refused"):
        comment = _add(scenario, scenario.member, "sem menção")
        with acting_as(scenario.member, scenario.a) as actor, pytest.raises(InvalidInput):
            CommentService().edit_comment(
                actor=actor,
                table_key=TABLE,
                record_id=scenario.note.pk,
                comment_id=comment.sysid,
                body=f"agora {token('X', scenario.denied.pk)}",
                expected_revision=comment.revision,
            )


def test_revocation_before_the_fan_out_stops_the_delivery(scenario):
    from skeleton.access import services as access_services
    from skeleton.access.models import TablePermission

    _add(scenario, scenario.admin_a, f"oi {token('Membro', scenario.member.pk)}")
    with acting_as(scenario.admin_a, scenario.a) as admin:
        for grant in TablePermission.objects.filter(table__model_label=TABLE, action="read"):
            access_services.revoke_link(grant, user=admin)
    run_pending_jobs()
    assert _received(scenario, scenario.member) == []


def test_picker_lists_only_reader_members(scenario):
    client = Client()
    client.post("/accounts/login/", {"login": scenario.member.email, "password": FIXTURE_PASSWORD})
    url = f"/api/v1/o/{scenario.a.pk}/tables/{TABLE}/records/{scenario.note.pk}/comments/mentionable/"
    people = {row["sysid"] for row in client.get(url).json()["results"]}
    assert str(scenario.member.pk) in people and str(scenario.admin_a.pk) in people
    assert not {str(scenario.denied.pk), str(scenario.outsider.pk), str(scenario.admin_b.pk)} & people
    assert all("@" not in row["name"] for row in client.get(url).json()["results"])
    assert (
        client.get(
            f"/api/v1/o/{scenario.b.pk}/tables/{TABLE}/records/{scenario.note.pk}/comments/mentionable/"
        ).status_code
        == 404
    )
