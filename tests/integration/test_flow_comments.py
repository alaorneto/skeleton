"""The business record's comments on the flow task and instance pages (#421)."""

import pytest
from django.urls import reverse

from skeleton.system.registry import CommentPolicy
from skeleton.testing.registries import table_spec_override
from skeleton.testing.service_api import FIXTURE_PASSWORD
from tests import support
from tests.integration.test_flow_process_manager import grant
from tests.integration.test_flow_service_api import my_task, scenario, start

pytestmark = pytest.mark.needs_example_app
__all__ = ["scenario"]
TABLE = "example_notes.note"
PANEL = 'id="comments-heading"'


def client(user):
    return support.login(user, password=FIXTURE_PASSWORD, csrf=False)


@pytest.fixture
def commented():
    with table_spec_override(TABLE, comments=CommentPolicy()):
        yield


def test_who_reads_the_record_comments_from_the_task_page(scenario, commented):
    receipt = start(scenario)
    task = my_task(scenario)
    member = client(scenario.member)
    task_url = reverse("flows:task", args=[scenario.a.pk, task.sysid])
    assert PANEL in member.get(task_url).content.decode()
    add = reverse("comments:add", args=[scenario.a.pk, TABLE, scenario.note.pk])
    answer = member.post(add, {"body": "Falta o anexo <b>", "next": task_url})
    assert (answer.status_code, answer.url) == (302, task_url)
    page = member.get(task_url).content.decode()
    assert "Falta o anexo &lt;b&gt;" in page and "Falta o anexo <b>" not in page
    instance_page = member.get(reverse("flows:instance", args=[scenario.a.pk, receipt.sysid])).content.decode()
    assert PANEL in instance_page and "Falta o anexo &lt;b&gt;" in instance_page


def test_the_return_address_stays_on_the_flow_pages(scenario, commented):
    member = client(scenario.member)
    add = reverse("comments:add", args=[scenario.a.pk, TABLE, scenario.note.pk])
    record = reverse(
        "web:record", kwargs={"organization_id": scenario.a.pk, "table_key": TABLE, "sysid": scenario.note.pk}
    )
    for target in ("https://elsewhere.test/", f"/o/{scenario.a.pk}/audit/", f"/o/{scenario.b.pk}/flows/", "//x.test/"):
        answer = member.post(add, {"body": "Ok", "next": target})
        assert (answer.status_code, answer.url) == (302, record), target


def test_without_reading_the_record_or_without_policy_there_is_no_panel(scenario, commented):
    receipt = start(scenario)
    grant(scenario, scenario.denied)  # Operates every instance, but cannot read notes.
    manager = client(scenario.denied)
    page = manager.get(reverse("flows:instance", args=[scenario.a.pk, receipt.sysid]))
    assert page.status_code == 200 and PANEL not in page.content.decode()
    add = reverse("comments:add", args=[scenario.a.pk, TABLE, scenario.note.pk])
    assert manager.post(add, {"body": "Oi"}).status_code == 404


def test_a_table_without_comment_policy_shows_no_panel(scenario):
    start(scenario)
    task = my_task(scenario)
    page = client(scenario.member).get(reverse("flows:task", args=[scenario.a.pk, task.sysid]))
    assert page.status_code == 200 and PANEL not in page.content.decode()
