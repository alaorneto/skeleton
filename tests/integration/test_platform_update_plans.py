"""Plans awaiting approval (#821), the two update areas (#822) and the steps of one plan (#823)."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.urls import reverse

from skeleton.accounts.platform import grant_platform_role
from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system import releases
from skeleton.system.models import DeploymentOperation
from skeleton.tenancy.context import request_context, system_context
from skeleton.web.platform import updates
from tests import support

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Update-plans-Password!821"
CORE, APP, OTHER = "c" * 64, "a" * 64, "d" * 64


@pytest.fixture
def admin():
    user, _ = bootstrap_identity(
        email="update-plans@example.test", organization="update-plans", name="Plans", password=PASSWORD
    )
    return user


def auditor_of(admin, email):
    """A platform user without ``updates.operate``."""
    with system_context(reason="test:update-plans-auditor"):
        auditor = create_identity(email=email, password=PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=auditor, email=auditor.email, verified=True, primary=True)
    with request_context(actor=admin, request_id=uuid4(), source="test:update-plans"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    return auditor


def plan(plan_id, *, action="core_update", artifacts=(CORE,), app_ids=(), generation=3, digest=None, **extra):
    components = [
        {"component_id": "django-skeleton", "version": "0.9.0", "digest": CORE, "publisher": "skeleton"},
        {"component_id": "org.example.notes", "version": "1.1.0", "digest": APP, "publisher": "acme"},
        {"component_id": "org.example.calc", "version": "2.0.0", "digest": OTHER, "publisher": "acme"},
    ]
    return {
        "id": plan_id,
        "action": action,
        "generation": generation,
        "digest": digest or uuid4().hex * 2,
        "components": components,
        "artifacts": list(artifacts),
        "app_ids": list(app_ids),
        "files": [],
        "migrations": [],
        **extra,
    }


def record(user, monkeypatch, value):
    """``record_preview`` of a ready plan answered by an agent double."""
    double = SimpleNamespace(plan=lambda plan_id: {"id": value["id"], "state": "ready", "plan": value})
    monkeypatch.setattr(releases, "agent_client", lambda: double)
    with request_context(actor=user, request_id=uuid4(), source="test:update-plans"):
        return releases.record_preview(user=user, plan_id=value["id"])


def state(row):
    with system_context(reason="test:update-plans-state"):
        return DeploymentOperation.objects.get(pk=row.pk).state


def test_the_scope_of_a_plan_is_what_it_was_asked_for():
    core = plan(str(uuid4()))
    assert releases.plan_scope(core) == {"django-skeleton"}
    assert releases.plan_scope(plan(str(uuid4()), action="update", artifacts=[APP])) == {"org.example.notes"}
    assert releases.plan_scope(plan(str(uuid4()), action="uninstall", artifacts=[], app_ids=["x.y"])) == {"x.y"}
    assert releases.plan_scope({"components": "nope", "artifacts": 3}) == set()
    assert releases.plan_scope(None) == set()


def test_a_new_plan_replaces_the_open_one_of_the_same_components(admin, monkeypatch, subtests):
    first = record(admin, monkeypatch, plan(str(uuid4())))
    app = record(admin, monkeypatch, plan(str(uuid4()), action="update", artifacts=[APP]))
    with subtests.test("another component stays open"):
        assert state(first) == "preview" and state(app) == "preview"
    second = record(admin, monkeypatch, plan(str(uuid4())))
    with subtests.test("the same component: the first is superseded"):
        assert state(first) == "superseded" and second.replaced == 1
        assert state(second) == "preview" and state(app) == "preview"
    with subtests.test("a plan of a newer generation expires the older ones"):
        newer = record(admin, monkeypatch, plan(str(uuid4()), action="update", artifacts=[OTHER], generation=4))
        assert state(app) == "stale" and state(second) == "stale" and state(newer) == "preview"
        assert newer.replaced == 0
    with subtests.test("recording the same plan again keeps one row"):
        value = plan(str(uuid4()), generation=4, digest="f" * 64)
        once, again = record(admin, monkeypatch, value), record(admin, monkeypatch, value)
        assert once.pk == again.pk and DeploymentOperation.objects.filter(plan_digest="f" * 64).count() == 1


def test_a_finished_operation_expires_the_plans_prepared_before_it(admin, monkeypatch):
    open_plan = record(admin, monkeypatch, plan(str(uuid4()), action="update", artifacts=[APP]))
    with request_context(actor=admin, request_id=uuid4(), source="test:update-plans"):
        running = DeploymentOperation.objects.create(
            operation="core_update",
            state="running",
            stage="migrated",
            expected_generation=3,
            plan_digest="9" * 64,
            plan={"id": str(uuid4())},
        )
        row = DeploymentOperation.objects.select_for_update().get(pk=running.pk)
        releases.reconcile_operation(
            row,
            {
                "id": str(row.pk),
                "operation": row.operation,
                "action": row.operation,
                "expected_generation": 3,
                "generation": 4,
                "plan_digest": row.plan_digest,
                "state": "succeeded",
                "stage": "ready",
            },
        )
    assert state(running) == "succeeded" and state(open_plan) == "stale"


def test_discarding_a_plan_has_one_winner_and_only_from_preview(admin, monkeypatch):
    row = record(admin, monkeypatch, plan(str(uuid4())))
    with request_context(actor=admin, request_id=uuid4(), source="test:update-plans"):
        releases.discard_preview(user=admin, operation_id=row.pk, revision=row.revision)
        with pytest.raises(ConcurrentUpdate):  # The same revision again: someone else already won.
            releases.discard_preview(user=admin, operation_id=row.pk, revision=row.revision)
        current = DeploymentOperation.objects.get(pk=row.pk)
        with pytest.raises(ConcurrentUpdate):  # A closed plan is never discarded again.
            releases.discard_preview(user=admin, operation_id=row.pk, revision=current.revision)
    assert state(row) == "discarded"


def test_the_discard_button_needs_csrf_recent_authentication_and_the_capability(admin, monkeypatch, subtests):
    from skeleton.audit.models import AuditEvent

    row = record(admin, monkeypatch, plan(str(uuid4())))
    url = reverse("platform:updates-discard", args=[row.pk])
    with subtests.test("without CSRF"):
        strict = support.login(admin, password=PASSWORD)
        assert strict.post(url, {"revision": row.revision}).status_code == 403
    with subtests.test("without the capability"):
        denied = support.login(auditor_of(admin, "plans-auditor@example.test"), password=PASSWORD, csrf=False)
        assert denied.post(url, {"revision": row.revision}).status_code == 403
    with subtests.test("without a recent authentication"):
        client = support.login(admin, password=PASSWORD, csrf=False)
        session = client.session
        session["account_authentication_methods"][-1]["at"] = 0
        session.save()
        response = client.post(url, {"revision": row.revision})
        assert response["Location"].startswith(reverse("account_reauthenticate"))
    with subtests.test("a forged revision is refused without writing"):
        client = support.login(admin, password=PASSWORD, csrf=False)
        assert client.post(url, {"revision": "x"}).status_code == 400
        response = client.post(url, {"revision": row.revision + 5})
        assert response["Location"] == reverse("platform:updates-operation", args=[row.pk])
    assert state(row) == "preview"
    with subtests.test("discarded, audited and back to the area"):
        response = client.post(url, {"revision": row.revision}, follow=True)
        assert response.redirect_chain[0][0] == reverse("platform:updates") and state(row) == "discarded"
        assert "Plano descartado" in response.content.decode()
        with request_context(actor=admin, request_id=uuid4(), source="test:update-plans-audit"):
            events = AuditEvent.objects.filter(record_sysid=row.pk, operation="update")
            assert [event.changes["state"]["new"] for event in events] == ["discarded"]


def operation_agent(monkeypatch, *, generation=3, ticket="https://status.example.test/t/abc"):
    calls = []

    def status_ticket(operation_id):
        calls.append(("status_ticket", operation_id))
        return {"status_url": ticket}

    def operation(operation_id):
        calls.append(("operation", operation_id))
        raise AssertionError("A plan awaiting approval or closed is never asked to the agent.")

    double = SimpleNamespace(
        inventory=lambda: {
            "generation": generation,
            "core": {"version": "0.8.0"},
            "apps": [{"state": "installed", "manifest": {"app_id": "org.example.notes", "version": "1.0.0"}}],
        },
        status_ticket=status_ticket,
        operation=operation,
        update_status=lambda: {},
        cluster=lambda: {"mode": "single"},
    )
    monkeypatch.setattr(releases, "agent_client", lambda: double)
    monkeypatch.setattr(updates, "agent_client", lambda: double)
    return calls


def test_the_approval_page_summarizes_and_asks_the_ticket_only_on_the_click(admin, monkeypatch, subtests):
    value = plan(str(uuid4()), rollout_mode="rolling", migrations=[{"operations": [{"reversible": False}]}])
    row = record(admin, monkeypatch, value)
    calls = operation_agent(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    page = client.get(reverse("platform:updates-operation", args=[row.pk]))
    body = page.content.decode()
    with subtests.test("what changes, in plain language"):
        changes = page.context["summary"]["changes"]
        assert {"name": "Núcleo", "before": "0.8.0", "after": "0.9.0", "kind": "update"} in changes
        assert any(change["kind"] == "install" and change["after"] == "2.0.0" for change in changes)
        assert "cada nó troca de versão por vez" in body and "Algumas não podem ser desfeitas" in body
        assert "Haverá manutenção e reinício" not in body
    with subtests.test("the steps of the plan"):
        assert 'aria-current="step"' in body and "Aprovar e iniciar" in body and "Descartar plano" in body
    with subtests.test("no ticket while rendering"):
        assert calls == []
    with subtests.test("the click asks for it and goes there"):
        response = client.post(reverse("platform:updates-status-link", args=[row.pk]))
        assert response.status_code == 302 and response["Location"] == "https://status.example.test/t/abc"
        assert calls == [("status_ticket", str(row.pk))]
    with subtests.test("an answer that is not a web address is refused"):
        operation_agent(monkeypatch, ticket="javascript:alert(1)")
        response = client.post(reverse("platform:updates-status-link", args=[row.pk]))
        assert response["Location"] == reverse("platform:updates-operation", args=[row.pk])


def test_the_status_link_needs_csrf_and_the_capability(admin, monkeypatch):
    row = record(admin, monkeypatch, plan(str(uuid4())))
    calls = operation_agent(monkeypatch)
    url = reverse("platform:updates-status-link", args=[row.pk])
    assert support.login(admin, password=PASSWORD).post(url).status_code == 403
    denied = support.login(auditor_of(admin, "ticket-auditor@example.test"), password=PASSWORD, csrf=False)
    assert denied.post(url).status_code == 403
    client = support.login(admin, password=PASSWORD, csrf=False)
    assert client.get(url).status_code == 405
    assert client.post(reverse("platform:updates-status-link", args=[uuid4()])).status_code == 404
    assert calls == []


def test_an_expired_plan_offers_no_approval(admin, monkeypatch):
    row = record(admin, monkeypatch, plan(str(uuid4())))
    operation_agent(monkeypatch, generation=4)
    client = support.login(admin, password=PASSWORD, csrf=False)
    body = client.get(reverse("platform:updates-operation", args=[row.pk])).content.decode()
    assert "Este plano venceu" in body and "Aprovar e iniciar" not in body
    assert "Preparar de novo" in body and "Descartar plano" in body
    assert state(row) == "preview"  # The page only reads.


def test_a_closed_plan_is_history_and_never_asks_the_agent(admin, monkeypatch):
    row = record(admin, monkeypatch, plan(str(uuid4())))
    with request_context(actor=admin, request_id=uuid4(), source="test:update-plans"):
        releases.discard_preview(user=admin, operation_id=row.pk, revision=row.revision)
    calls = operation_agent(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    body = client.get(reverse("platform:updates-operation", args=[row.pk])).content.decode()
    assert "foi descartado e nunca foi aplicado" in body and "Aprovar e iniciar" not in body
    assert calls == []


def test_the_history_separates_the_areas_and_the_closed_plans(admin, monkeypatch, subtests):
    core = record(admin, monkeypatch, plan(str(uuid4())))
    app = record(admin, monkeypatch, plan(str(uuid4()), action="update", artifacts=[APP]))
    with request_context(actor=admin, request_id=uuid4(), source="test:update-plans"):
        releases.discard_preview(user=admin, operation_id=app.pk, revision=app.revision)
    client = support.login(admin, password=PASSWORD, csrf=False)
    url = reverse("platform:updates-operations")

    def listed(**query):
        return {row.pk for row in client.get(url, query).context["operations"]}

    with subtests.test("platform"):
        assert listed(area="platform") == {core.pk}
    with subtests.test("apps: the discarded plan is hidden by default"):
        assert listed(area="apps") == set()
        assert listed(area="apps", estado="encerradas") == {app.pk}
    with subtests.test("the overview of the platform lists only its open plan"):
        operation_agent(monkeypatch)
        page = client.get(reverse("platform:updates"))
        assert [row.pk for row in page.context["pending"]] == [core.pk]
        assert "Revisar e aprovar" in page.content.decode()


class AppsAgent:
    """The agent of the apps area: an installed app, a retained one and a project-local one."""

    def __init__(self):
        self.calls = []

    def inventory(self):
        return {
            "generation": 3,
            "core": {"version": "0.8.0"},
            "apps": [
                {"state": "installed", "digest": APP, "manifest": {"app_id": "org.example.notes", "version": "1.0.0"}},
                {"state": "schema_retained", "digest": OTHER, "manifest": {"app_id": "org.example.calc"}},
                {"state": "installed", "origin": "project-local", "manifest": {"app_id": "project.local"}},
            ],
        }

    def update_status(self):
        return {"paths": [], "releases": [], "sources": []}

    def preview(self, action, artifacts=None, app_ids=None):
        self.calls.append((action, artifacts, app_ids))
        return {"id": str(uuid4()), "state": "preparing"}


def test_the_apps_area_acts_without_typing_a_digest_or_an_id(admin, monkeypatch, subtests):
    double = AppsAgent()
    monkeypatch.setattr(releases, "agent_client", lambda: double)
    monkeypatch.setattr(updates, "agent_client", lambda: double)
    client = support.login(admin, password=PASSWORD, csrf=False)
    body = client.get(reverse("platform:updates-apps")).content.decode()
    with subtests.test("installed: take the code out; retained: reinstall; project code: nothing"):
        assert 'name="app_ids" value="org.example.notes"' in body
        assert f'name="action" value="reinstall"><input type="hidden" name="artifacts" value="{OTHER}"' in body
        assert 'value="project.local"' not in body and "Código do projeto" in body
    with subtests.test("the buttons reach the preview of the right plan"):
        client.post(reverse("platform:updates-plan"), {"action": "uninstall", "app_ids": "org.example.notes"})
        client.post(reverse("platform:updates-plan"), {"action": "reinstall", "artifacts": OTHER})
        assert double.calls == [("uninstall", [], ["org.example.notes"]), ("reinstall", [OTHER], [])]
    with subtests.test("the area needs the capability"):
        denied = support.login(auditor_of(admin, "apps-auditor@example.test"), password=PASSWORD, csrf=False)
        assert denied.get(reverse("platform:updates-apps")).status_code == 403


def test_an_uploaded_app_package_updates_the_installed_app(admin, monkeypatch):
    from django.core.files.uploadedfile import SimpleUploadedFile

    double = AppsAgent()
    double.upload = lambda path: {"digest": APP, "kind": "app", "manifest": {"app_id": "org.example.notes"}}
    monkeypatch.setattr(releases, "agent_client", lambda: double)
    monkeypatch.setattr(updates, "agent_client", lambda: double)
    client = support.login(admin, password=PASSWORD, csrf=False)
    package = SimpleUploadedFile("app.skapp", b"PK\x05\x06" + b"\0" * 18)
    response = client.post(reverse("platform:updates-upload"), {"package": package, "area": "apps"})
    assert response.context["plan_form"].initial == {"artifacts": APP, "action": "update"}
    body = response.content.decode()
    assert "Conferir o impacto" in body and response.context["platform_section"] == "updates-apps"
    assert not response.context["plan_manual"]  # The summary and one button, the form in "Avançado".
