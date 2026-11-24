"""The configurable internal home of an organization (#624), on PostgreSQL with the runtime role."""

import re

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system import dashboard_widgets
from skeleton.system.dashboard_widgets import DashboardWidget
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario
from tests.support import login

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
UNAVAILABLE = "Este quadro não está disponível agora."
# Queries of the internal home with the default document for a member, recorded on 2026-11-21:
# the shell, the home page row, the person's tasks and unread notifications of this organization.
# 31 since #740: the organization settings row is read once per request (the MFA gate reuses it).
DEFAULT_HOME_QUERIES = 31


@pytest.fixture
def scenario():
    from examples.notes.models import Note

    return build_service_scenario(Note, slug="home-page")


def home(organization):
    return reverse("web:dashboard", kwargs={"organization_id": organization.pk})


def save(scenario, document, *, published=True, revision=0, user=None, organization=None):
    from skeleton.tenancy.pages import save_organization_page

    with acting_as(user or scenario.admin_a, organization or scenario.a) as actor:
        return save_organization_page(
            actor=actor, slot="home", document=document, published=published, expected_revision=revision
        )


def headings(html):
    content = html.split('id="conteudo"', 1)[1]
    # The membership block is a danger zone (#769): its heading counts too.
    return re.findall(r'<h2[^>]*class="(?:section-heading|danger-zone-title)"[^>]*>([^<]+)</h2>', content)


def tasks_for(scenario, organization, count, prefix):
    from examples.notes.service_api import NoteService

    with acting_as(scenario.member, organization) as actor:
        for index in range(count):
            note = NoteService().create_note(actor=actor, code=f"{prefix}-{index}", title=f"{prefix} {index}")
            NoteService().request_publication(actor=actor, note_id=note.sysid)


def test_without_a_published_page_the_home_shows_the_default(scenario):
    client = login(scenario.member, password=FIXTURE_PASSWORD, csrf=False)
    response = client.get(home(scenario.a))
    assert response.status_code == 200
    assert headings(response.content.decode()) == ["Aplicativos", "Minhas tarefas", "Notificações", "Seu vínculo"]
    html = response.content.decode()
    assert reverse("flows:tasks", kwargs={"organization_id": scenario.a.pk}) in html
    assert reverse("notifications:organization", kwargs={"organization_id": scenario.a.pk}) in html
    state = save(scenario, {"version": 1, "blocks": [{"type": "text", "body": "Rascunho"}]}, published=False)
    assert "Rascunho" not in client.get(home(scenario.a)).content.decode()  # A draft is never shown.
    assert state.revision == 1


def test_the_published_page_keeps_its_order_and_shows_only_the_person_s_rows(scenario):
    from skeleton.notifications.registry import NotificationType, register_notification_type
    from skeleton.notifications.services import Recipients, notify
    from skeleton.testing.jobs import run_pending_jobs
    from skeleton.testing.registries import isolated_notification_types

    tasks_for(scenario, scenario.a, 6, "a")
    tasks_for(scenario, scenario.b, 1, "b")
    with isolated_notification_types():
        for index in range(6):
            register_notification_type(NotificationType(f"home.notice{index}", f"Aviso A{index}"))
        register_notification_type(NotificationType("home.other", "Aviso de Beta"))
        with acting_as(scenario.admin_a, scenario.a):
            for index in range(6):
                notify(f"home.notice{index}", recipients=Recipients(users=(scenario.member.pk,)))
            notify("home.notice0", recipients=Recipients(users=(scenario.admin_a.pk,)))
        with acting_as(scenario.admin_b, scenario.b):
            notify("home.other", recipients=Recipients(users=(scenario.member.pk,)))
        run_pending_jobs()
        save(
            scenario,
            {
                "version": 1,
                "blocks": [
                    {"type": "notifications", "heading": "Avisos"},
                    {"type": "text", "heading": "Boas-vindas", "body": "<em>Olá</em>"},
                    {"type": "tasks", "heading": ""},
                ],
            },
        )
        client = login(scenario.member, password=FIXTURE_PASSWORD, csrf=False)
        html = client.get(home(scenario.a)).content.decode()
    assert headings(html) == ["Avisos", "Boas-vindas", "Minhas tarefas", "Seu vínculo"]
    assert "&lt;em&gt;Olá&lt;/em&gt;" in html
    content = html.split('id="conteudo"', 1)[1]  # The header's switcher lists B; the home never shows its rows.
    assert len(re.findall(rf'href="/o/{scenario.a.pk}/flows/tasks/[0-9a-f-]{{36}}/"', content)) == 5
    assert f'href="/o/{scenario.a.pk}/flows/tasks/"' in content  # "See all tasks".
    assert len(re.findall(r"/notifications/[0-9a-f-]{36}/open/", content)) == 5
    assert "Aviso de Beta" not in content and f"/o/{scenario.b.pk}/" not in content


def test_widgets_of_apps_disappear_or_degrade_without_breaking_the_page(scenario, monkeypatch, subtests):
    registry = dict(dashboard_widgets._registry)
    monkeypatch.setattr(dashboard_widgets, "_registry", registry)

    def broken(**kwargs):
        raise RuntimeError("secret detail")

    registry["example_notes.broken"] = DashboardWidget(
        "example_notes.broken", "example_notes", "Quadro com erro", "example_notes/widgets/recent.html", broken
    )
    scenario.create(scenario.a, code="w-1", title="Nota do quadro")
    save(
        scenario,
        {
            "version": 1,
            "blocks": [
                {"type": "widget", "name": "example_notes.recent"},
                {"type": "widget", "name": "example_notes.broken"},
                {"type": "text", "heading": "Depois", "body": "Fim"},
            ],
        },
    )
    with subtests.test("active"):
        html = login(scenario.member, password=FIXTURE_PASSWORD, csrf=False).get(home(scenario.a)).content.decode()
        assert headings(html)[:3] == ["Anotações recentes", "Quadro com erro", "Depois"]
        assert "Nota do quadro" in html and UNAVAILABLE in html and "secret detail" not in html
    with subtests.test("gate not passed"):
        response = login(scenario.denied, password=FIXTURE_PASSWORD, csrf=False).get(home(scenario.a))
        assert response.status_code == 200
        assert "Anotações recentes" not in response.content.decode() and "Depois" in response.content.decode()
    with subtests.test("app disabled"), scenario.application_disabled():
        response = login(scenario.member, password=FIXTURE_PASSWORD, csrf=False).get(home(scenario.a))
        assert response.status_code == 200
        assert headings(response.content.decode()) == ["Depois", "Seu vínculo"]


def test_only_the_administrator_edits_with_cas_and_sees_the_widgets(scenario, subtests):
    from skeleton.testing.people import add_member, make_person

    manager = make_person(email="manager@home-page.example.test")
    add_member(organization=scenario.a, user=manager, role_code="people_manager")
    editor = reverse("web:organization-page-home", kwargs={"organization_id": scenario.a.pk})
    for who, user in (("member", scenario.member), ("delegated manager", manager)):
        with subtests.test(who):
            client = login(user, password=FIXTURE_PASSWORD)
            assert client.get(editor).status_code == 404
            with pytest.raises(PermissionDenied):
                save(scenario, {"version": 1, "blocks": [{"type": "apps"}]}, user=user)
    with subtests.test("administrator"):
        admin = login(scenario.admin_a, password=FIXTURE_PASSWORD)
        assert admin.get(editor).status_code == 200
        form = admin.get(
            reverse("web:organization-page-home-add", kwargs={"organization_id": scenario.a.pk}) + "?type=widget"
        )
        assert 'value="example_notes.recent"' in form.content.decode()
        token = admin.cookies["csrftoken"].value
        added = admin.post(
            reverse("web:organization-page-home-add", kwargs={"organization_id": scenario.a.pk}) + "?type=widget",
            {"name": "example_notes.recent", "revision": 0, "csrfmiddlewaretoken": token},
        )
        assert added.status_code == 302
        preview = admin.get(reverse("web:organization-page-home-preview", kwargs={"organization_id": scenario.a.pk}))
        assert preview.status_code == 200 and "Anotações recentes" in preview.content.decode()
        stale = admin.post(
            reverse("web:organization-page-home-publish", kwargs={"organization_id": scenario.a.pk}),
            {"published": "on", "revision": 0, "csrfmiddlewaretoken": token},
        )
        assert stale.status_code == 409
    with subtests.test("service CAS"), pytest.raises(ConcurrentUpdate):
        save(scenario, {"version": 1, "blocks": [{"type": "apps"}]}, revision=0)
    with subtests.test("a widget of an app turned off stays valid in the document"), scenario.application_disabled():
        save(scenario, {"version": 1, "blocks": [{"type": "widget", "name": "example_notes.recent"}]}, revision=1)


def test_the_default_home_has_a_fixed_query_budget(scenario):
    client = login(scenario.member, password=FIXTURE_PASSWORD, csrf=False)
    client.get(home(scenario.a))  # Warm the per-process caches (translations, templates).
    with CaptureQueriesContext(connection) as captured:
        assert client.get(home(scenario.a)).status_code == 200
    tasks_for(scenario, scenario.a, 3, "budget")
    with CaptureQueriesContext(connection) as more:
        client.get(home(scenario.a))
    assert len(captured) == len(more) == DEFAULT_HOME_QUERIES
