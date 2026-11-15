"""Release packages in /platform/updates/ (#495): planned through the core package they carry."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse

from skeleton.accounts.platform import grant_platform_role
from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.system.releases import plan_artifact
from skeleton.tenancy.context import request_context, system_context
from skeleton.web.platform import updates
from tests import support

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Release-package-Password!495"
RELEASE_DIGEST, CORE_DIGEST = "a" * 64, "b" * 64


def release_result(target="cp314-linux-x86_64"):
    return {
        "digest": RELEASE_DIGEST,
        "kind": "release",
        "manifest": {"kind": "release", "name": "Skeleton", "version": "0.6.0"},
        "core": {"digest": CORE_DIGEST, "manifest": {"kind": "core", "version": "0.6.0"}},
        "target": target,
        "agent": {"version": "0.6.0"},
    }


@pytest.fixture
def admin():
    user, _ = bootstrap_identity(
        email="release-package@example.test", organization="release-package", name="Release", password=PASSWORD
    )
    return user


def post_upload(client, monkeypatch, result):
    calls = []

    def upload(path):
        calls.append(path)
        return result

    monkeypatch.setattr(updates, "agent_client", lambda: SimpleNamespace(upload=upload))
    package = SimpleUploadedFile("release.skapp", b"PK\x05\x06" + b"\0" * 18, content_type="application/octet-stream")
    return client.post(reverse("platform:updates-upload"), {"package": package}), calls


def test_plan_artifact_of_each_kind():
    assert plan_artifact(release_result()) == (CORE_DIGEST, "core_update")
    assert plan_artifact({"digest": RELEASE_DIGEST, "kind": "app", "manifest": {}}) == (RELEASE_DIGEST, None)
    assert plan_artifact({"digest": RELEASE_DIGEST, "manifest": {}}) == (RELEASE_DIGEST, None)


def test_release_upload_opens_a_core_update_plan(admin, monkeypatch):
    client = support.login(admin, password=PASSWORD, csrf=False)
    response, calls = post_upload(client, monkeypatch, release_result())
    assert response.status_code == 200 and len(calls) == 1
    form = response.context["plan_form"]
    assert form.initial == {"artifacts": CORE_DIGEST, "action": "core_update"}
    body = response.content.decode()
    assert CORE_DIGEST in body
    assert "não traz dependências para a plataforma deste servidor" not in body


def test_release_without_this_target_warns_about_the_operator_lock(admin, monkeypatch):
    client = support.login(admin, password=PASSWORD, csrf=False)
    response, _ = post_upload(client, monkeypatch, release_result(target=None))
    assert response.status_code == 200
    assert "Valem o lock e o wheelhouse configurados no agente." in response.content.decode()


def test_app_upload_keeps_the_install_plan(admin, monkeypatch):
    client = support.login(admin, password=PASSWORD, csrf=False)
    app = {"digest": RELEASE_DIGEST, "kind": "app", "manifest": {"name": "Notes", "version": "1.0.0"}}
    response, _ = post_upload(client, monkeypatch, app)
    assert response.context["plan_form"].initial == {"artifacts": RELEASE_DIGEST, "action": "install"}


def test_upload_still_requires_the_release_capability(admin, monkeypatch):
    with system_context(reason="test:release-package-auditor"):
        auditor = create_identity(email="release-auditor@example.test", password=PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=auditor, email=auditor.email, verified=True, primary=True)
    with request_context(actor=admin, request_id=uuid4(), source="test:release-package"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    client = support.login(auditor, password=PASSWORD, csrf=False)
    response, calls = post_upload(client, monkeypatch, release_result())
    assert response.status_code == 403 and calls == []


def test_plan_details_list_dropped_dependencies():
    plan = {"components": [], "files": [], "resources": [], "presentation_changes": [], "migrations": []}
    assert "deixam de ser instaladas" not in render_to_string(
        "skeleton/web/platform/updates/plan-details.html", {"plan": plan}
    )
    text = render_to_string(
        "skeleton/web/platform/updates/plan-details.html", {"plan": {**plan, "dropped_dependencies": ["redis"]}}
    )
    assert "Dependências que deixam de ser instaladas" in text and "<code>redis</code>" in text


def agent_double(monkeypatch, *, self_update=True, calls=None):
    calls = [] if calls is None else calls

    def update_agent(digest, *, actor_id):
        calls.append((digest, actor_id))
        return {"state": "restarting", "version": "0.7.0"}

    inventory = {
        "generation": 1,
        "apps": [],
        "core": {"version": "0.6.0"},
        "agent": {"version": "0.6.0", "self_update": self_update},
    }
    client = SimpleNamespace(update_agent=update_agent, inventory=lambda: inventory, upload=lambda path: offer())
    from skeleton.system import releases

    monkeypatch.setattr(releases, "agent_client", lambda: client)
    monkeypatch.setattr(updates, "agent_client", lambda: client)
    return calls


def offer():
    return {**release_result(), "agent": {"version": "0.7.0", "update_required": True}}


def test_installed_versions_show_the_agent(admin, monkeypatch):
    agent_double(monkeypatch, self_update=False)
    client = support.login(admin, password=PASSWORD, csrf=False)
    body = client.get(reverse("platform:updates")).content.decode()
    assert "Agente de deployment" in body and "Atualização do agente: manual (veja o guia)" in body


def test_release_upload_offers_the_agent_update(admin, monkeypatch):
    agent_double(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    package = SimpleUploadedFile("release.skapp", b"PK\x05\x06" + b"\0" * 18)
    body = client.post(reverse("platform:updates-upload"), {"package": package}).content.decode()
    assert "Atualizar agente" in body and reverse("platform:updates-agent") in body
    agent_double(monkeypatch, self_update=False)
    package = SimpleUploadedFile("release.skapp", b"PK\x05\x06" + b"\0" * 18)
    body = client.post(reverse("platform:updates-upload"), {"package": package}).content.decode()
    assert "Atualizar agente" not in body and "agent-install" in body


def test_agent_update_posts_the_digest_and_redirects(admin, monkeypatch):
    calls = agent_double(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = client.post(reverse("platform:updates-agent"), {"release": RELEASE_DIGEST})
    assert response.status_code == 302 and response["Location"] == reverse("platform:updates")
    assert calls == [(RELEASE_DIGEST, admin.pk)]
    assert client.post(reverse("platform:updates-agent"), {"release": "../x"}).status_code == 400
    assert client.get(reverse("platform:updates-agent")).status_code == 405
    assert len(calls) == 1


def test_agent_update_needs_csrf_recent_authentication_and_the_capability(admin, monkeypatch):
    calls = agent_double(monkeypatch)
    strict = support.login(admin, password=PASSWORD)
    assert strict.post(reverse("platform:updates-agent"), {"release": RELEASE_DIGEST}).status_code == 403
    session = strict.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = strict.post(
        reverse("platform:updates-agent"),
        {"release": RELEASE_DIGEST, "csrfmiddlewaretoken": strict.cookies["csrftoken"].value},
    )
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    with system_context(reason="test:agent-update-auditor"):
        auditor = create_identity(email="agent-auditor@example.test", password=PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=auditor, email=auditor.email, verified=True, primary=True)
    with request_context(actor=admin, request_id=uuid4(), source="test:agent-update"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    denied = support.login(auditor, password=PASSWORD, csrf=False)
    assert denied.post(reverse("platform:updates-agent"), {"release": RELEASE_DIGEST}).status_code == 403
    assert calls == []


class SourcesAgent:
    """The agent's sources API, recorded; ``refuse`` makes set_source fail with that code."""

    def __init__(self, *, publishers=("skeleton", "acme"), rows=(), refuse=None):
        self.calls, self.refuse = [], refuse
        self.listing = {"sources": list(rows), "publishers": list(publishers), "private_available": False, "limit": 16}

    def sources(self):
        return self.listing

    def set_source(self, **values):
        self.calls.append(("set", values))
        if self.refuse:
            from skeleton.system.deployment_client import AgentError

            raise AgentError(self.refuse, 409)
        return self.listing

    def remove_source(self, source_id, *, actor_id):
        self.calls.append(("remove", source_id, actor_id))
        return self.listing


def sources_agent(monkeypatch, **options):
    from skeleton.system import releases

    double = SourcesAgent(**options)
    monkeypatch.setattr(releases, "agent_client", lambda: double)
    return double


def test_sources_page_suggests_the_upstream_repository(admin, monkeypatch):
    sources_agent(monkeypatch, rows=[{"id": "local", "type": "path", "channels": ["stable"], "origin": "config"}])
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = client.get(reverse("platform:updates-sources"))
    assert response.status_code == 200
    form = response.context["source_form"]
    assert form.initial["repository"] == "alaorneto/skeleton" and form.initial["source_id"] == "skeleton"
    body = response.content.decode()
    assert "arquivo do agente" in body and "esta tela não cadastra chaves" in body
    assert [choice for choice, _ in form.fields["publisher"].choices] == ["skeleton", "acme"]
    assert "private" not in form.fields


def test_sources_page_without_publishers_explains_what_to_do(admin, monkeypatch):
    sources_agent(monkeypatch, publishers=())
    client = support.login(admin, password=PASSWORD, csrf=False)
    body = client.get(reverse("platform:updates-sources")).content.decode()
    assert "Nenhum publicador aprovado no servidor" in body


def test_save_and_remove_a_source(admin, monkeypatch):
    double = sources_agent(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    data = {"source_id": "fork", "repository": "acme/skeleton", "publisher": "acme", "channel": "stable"}
    response = client.post(reverse("platform:updates-sources"), data)
    assert response.status_code == 302 and response["Location"] == reverse("platform:updates-sources")
    assert double.calls == [
        (
            "set",
            {
                "id": "fork",
                "github": "acme/skeleton",
                "publisher": "acme",
                "channels": ["stable"],
                "private": False,
                "actor_id": admin.pk,
            },
        )
    ]
    response = client.post(reverse("platform:updates-sources-remove"), {"source_id": "fork"})
    assert response.status_code == 302 and double.calls[-1] == ("remove", "fork", admin.pk)


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": "https://github.com/acme/skeleton"},
        {"repository": "acme/skeleton/extra"},
        {"repository": "acme/../x"},
        {"publisher": "evil"},
        {"source_id": "Fork"},
    ],
)
def test_invalid_forms_never_reach_the_agent(admin, monkeypatch, changes):
    double = sources_agent(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    data = {"source_id": "fork", "repository": "acme/skeleton", "publisher": "acme", "channel": "stable", **changes}
    assert client.post(reverse("platform:updates-sources"), data).status_code == 400
    assert double.calls == []


def test_agent_refusal_is_translated(admin, monkeypatch):
    sources_agent(monkeypatch, refuse="publisher_not_approved")
    client = support.login(admin, password=PASSWORD, csrf=False)
    data = {"source_id": "fork", "repository": "acme/skeleton", "publisher": "acme", "channel": "stable"}
    response = client.post(reverse("platform:updates-sources"), data, follow=True)
    assert "não está aprovado no servidor" in response.content.decode()


def test_unavailable_agent_shows_the_standard_notice(admin, monkeypatch):
    from skeleton.system import releases

    def unavailable():
        raise releases.DeploymentUnavailable("not configured")

    monkeypatch.setattr(releases, "agent_client", unavailable)
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = client.get(reverse("platform:updates-sources"))
    assert response.status_code == 200 and "não está disponível" in response.content.decode()


def test_source_changes_need_csrf_recent_authentication_and_the_capability(admin, monkeypatch):
    double = sources_agent(monkeypatch)
    data = {"source_id": "fork", "repository": "acme/skeleton", "publisher": "acme", "channel": "stable"}
    strict = support.login(admin, password=PASSWORD)
    for name in ("platform:updates-sources", "platform:updates-sources-remove"):
        assert strict.post(reverse(name), data).status_code == 403
    session = strict.session
    session["account_authentication_methods"][-1]["at"] = 0
    session.save()
    response = strict.post(
        reverse("platform:updates-sources"), {**data, "csrfmiddlewaretoken": strict.cookies["csrftoken"].value}
    )
    assert response.status_code == 302 and response["Location"].startswith(reverse("account_reauthenticate"))
    with system_context(reason="test:sources-auditor"):
        auditor = create_identity(email="sources-auditor@example.test", password=PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=auditor, email=auditor.email, verified=True, primary=True)
    with request_context(actor=admin, request_id=uuid4(), source="test:sources"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    denied = support.login(auditor, password=PASSWORD, csrf=False)
    assert denied.get(reverse("platform:updates-sources")).status_code == 403
    assert denied.post(reverse("platform:updates-sources"), data).status_code == 403
    assert double.calls == []


STATUS = {
    "checked_at": "2026-11-15T12:00:00+00:00",
    "sources": [
        {"id": "official", "state": "available", "error_code": None},
        {"id": "fork", "state": "unavailable", "error_code": "github_rate_limited"},
    ],
    "releases": [
        {
            "source_id": "official",
            "component_id": "django-skeleton",
            "version": "0.8.0",
            "state": "available",
            "notes": "",
        }
    ],
    "paths": [
        {
            "component_id": "django-skeleton",
            "kind": "core",
            "installed": "0.5.2",
            "latest": "0.7.1",
            "steps": [{"version": "0.6.4", "source_id": "official"}, {"version": "0.7.1", "source_id": "official"}],
            "blocked": None,
        },
        {
            "component_id": "org.example.notes",
            "kind": "app",
            "installed": "1.0.0",
            "latest": "1.4.0",
            "steps": [],
            "blocked": "missing_intermediate_release",
        },
        {
            "component_id": "org.example.calc",
            "kind": "app",
            "installed": "2.0.0",
            "latest": "3.0.0",
            "steps": [],
            "blocked": "incompatible_runtime",
        },
        {
            "component_id": "org.example.done",
            "kind": "app",
            "installed": "1.0.0",
            "latest": None,
            "steps": [],
            "blocked": None,
        },
    ],
}


class PathAgent:
    def __init__(self, status=STATUS, *, agent=None, fetched=None):
        self.calls, self.status, self.fetched = [], status, fetched
        self.agent = agent or {"version": "0.7.0", "self_update": True}

    def update_status(self, **options):
        self.calls.append("update_status")
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def check_updates(self):
        self.calls.append("check_updates")
        return {}

    def inventory(self):
        self.calls.append("inventory")
        return {
            "generation": 1,
            "apps": [{"state": "installed", "manifest": {"app_id": "org.example.notes", "name": "Notas"}}],
            "agent": self.agent,
        }

    def fetch_release(self, **identifiers):
        self.calls.append(("fetch_release", identifiers))
        return self.fetched

    def preview(self, action, artifacts=None, app_ids=None):
        self.calls.append(("preview", action, artifacts))
        return {"id": "11111111-2222-4333-8444-555555555555", "state": "preparing"}


def path_agent(monkeypatch, **options):
    from skeleton.system import releases

    double = PathAgent(**options)
    monkeypatch.setattr(releases, "agent_client", lambda: double)
    monkeypatch.setattr(updates, "agent_client", lambda: double)
    return double


def test_available_shows_the_last_check_and_one_step_at_a_time(admin, monkeypatch, subtests):
    double = path_agent(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = client.get(reverse("platform:updates-available"))
    body = response.content.decode()
    with subtests.test("no new check"):
        assert "check_updates" not in double.calls and "update_status" in double.calls
    with subtests.test("steps in order, only the first with a button"):
        cards = response.context["paths"]
        assert [step["version"] for step in cards[0]["steps"]] == ["0.6.4", "0.7.1"]
        assert body.count("Preparar este passo") == 1 and "Depois: 0.7.1" in body
        assert body.index("0.6.4") < body.index("Depois: 0.7.1")
        assert 'name="version" value="0.6.4"' in body and 'name="source_id" value="official"' in body
    with subtests.test("blocked texts"):
        assert "não pode pular direto para 1.4.0" in body
        assert "não são compatíveis com o Python ou o núcleo" in body
        assert "Notas" in body  # The installed manifest's name.
    with subtests.test("up-to-date component has no button"):
        assert [card["component_id"] for card in response.context["current"]] == ["org.example.done"]
    with subtests.test("source error text and full table"):
        assert "O GitHub limitou as consultas" in body and "Todas as versões publicadas" in body
        assert "2026-11-15T12:00:00" in body


def test_check_now_redirects_to_the_status(admin, monkeypatch):
    double = path_agent(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = client.post(reverse("platform:updates-available"))
    assert response.status_code == 302 and response["Location"] == reverse("platform:updates-available")
    assert double.calls == ["check_updates"]


@pytest.mark.parametrize(
    "status", [{"paths": [{"steps": "x"}, "nope", {"component_id": 3}], "sources": [None]}, {}, []]
)
def test_unexpected_status_shapes_never_break_the_page(admin, monkeypatch, status):
    path_agent(monkeypatch, status=status)
    client = support.login(admin, password=PASSWORD, csrf=False)
    assert client.get(reverse("platform:updates-available")).status_code == 200


def test_unavailable_agent_on_available_shows_the_notice(admin, monkeypatch):
    from skeleton.system.releases import DeploymentUnavailable

    path_agent(monkeypatch, status=DeploymentUnavailable("x"))
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = client.get(reverse("platform:updates-available"))
    assert response.status_code == 200 and "não está disponível" in response.content.decode()


def release_fetch(core_version="0.7.1"):
    return {
        **release_result(),
        "manifest": {"kind": "release", "name": "Skeleton", "version": core_version},
        "core": {"digest": CORE_DIGEST, "manifest": {"kind": "core", "version": core_version}},
        "agent": {"version": core_version, "update_required": True},
    }


def fetch(client, component="django-skeleton", version="0.7.1"):
    return client.post(
        reverse("platform:updates-fetch"), {"component_id": component, "version": version, "source_id": "official"}
    )


def test_step_needing_a_newer_agent_stops_before_the_preview(admin, monkeypatch):
    double = path_agent(monkeypatch, agent={"version": "0.6.4", "self_update": True}, fetched=release_fetch())
    client = support.login(admin, password=PASSWORD, csrf=False)
    body = fetch(client).content.decode()
    assert "Atualize o agente primeiro" in body and "O agente instalado é 0.6.4" in body
    assert reverse("platform:updates-agent") in body and RELEASE_DIGEST in body
    assert not any(call[0] == "preview" for call in double.calls if isinstance(call, tuple))
    path_agent(monkeypatch, agent={"version": "0.6.4", "self_update": False}, fetched=release_fetch())
    body = fetch(client).content.decode()
    assert "agent-install" in body and reverse("platform:updates-agent") not in body


def test_step_with_a_current_agent_goes_to_the_plan(admin, monkeypatch):
    path_agent(monkeypatch, agent={"version": "0.7.0", "self_update": True}, fetched=release_fetch())
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = fetch(client)
    assert response.context["plan_form"].initial == {"action": "core_update", "artifacts": CORE_DIGEST}


def test_app_step_goes_straight_to_the_plan(admin, monkeypatch):
    app = {"digest": RELEASE_DIGEST, "kind": "app", "manifest": {"name": "Notas", "version": "1.4.0"}}
    path_agent(monkeypatch, fetched=app)
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = fetch(client, "org.example.notes", "1.4.0")
    assert response.context["plan_form"].initial == {"action": "update", "artifacts": RELEASE_DIGEST}


def test_jump_refusal_is_translated(admin, monkeypatch):
    from skeleton.system.deployment_client import AgentError

    class Refusing(PathAgent):
        def preview(self, action, artifacts=None, app_ids=None):
            raise AgentError("version_jump_forbidden", 409)

    from skeleton.system import releases

    double = Refusing()
    monkeypatch.setattr(releases, "agent_client", lambda: double)
    client = support.login(admin, password=PASSWORD, csrf=False)
    response = client.post(reverse("platform:updates-plan"), {"action": "core_update", "artifacts": CORE_DIGEST})
    assert "mais de uma linha à frente da instalada" in response.content.decode()


def finished_operation(admin, state="succeeded"):
    from skeleton.system.models import DeploymentOperation

    with request_context(actor=admin, request_id=uuid4(), source="test:next-step"):
        return DeploymentOperation.objects.create(
            operation="core_update",
            state=state,
            stage="ready",
            expected_generation=1,
            plan={"id": str(uuid4()), "components": [], "files": [], "resources": [], "migrations": []},
            plan_digest="c" * 64,
        )


def test_finished_operation_offers_the_next_step(admin, monkeypatch, subtests):
    row = finished_operation(admin)
    double = path_agent(monkeypatch)
    double.operation = lambda operation_id: {
        "id": str(row.pk),
        "state": "succeeded",
        "stage": "ready",
        "generation": 2,
        "expected_generation": 1,
        "plan_digest": row.plan_digest,
        "operation": row.operation,
        "action": row.operation,
    }
    client = support.login(admin, password=PASSWORD, csrf=False)
    url = reverse("platform:updates-operation", args=[row.pk])
    with subtests.test("next step"):
        body = client.get(url).content.decode()
        assert "Próximo passo" in body and "Preparar este passo" in body
    with subtests.test("nothing left"):
        double.status = {
            "paths": [{"component_id": "django-skeleton", "installed": "0.7.1", "latest": None, "steps": []}]
        }
        assert "Não há mais atualizações pendentes" in client.get(url).content.decode()
    with subtests.test("status failure only hides the section"):
        from skeleton.system.releases import DeploymentUnavailable

        double.status = DeploymentUnavailable("x")
        response = client.get(url)
        assert response.status_code == 200 and "Próximo passo" not in response.content.decode()


@pytest.fixture
def notice_cache(settings):
    from django.core.cache import cache

    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "banner"}}
    settings.SKELETON_AGENT_SOCKET = "/run/skeleton/agent.sock"
    cache.clear()
    yield cache
    cache.clear()


def notice_agent(monkeypatch, status=STATUS):
    from skeleton.system import releases

    double = PathAgent(status)
    monkeypatch.setattr(releases, "agent_client", lambda: double)
    monkeypatch.setattr(updates, "agent_client", lambda: double)
    return double


def test_platform_banner_for_release_operators(admin, monkeypatch, notice_cache, subtests):
    double = notice_agent(monkeypatch)
    client = support.login(admin, password=PASSWORD, csrf=False)
    with subtests.test("banner on platform pages"):
        body = client.get(reverse("platform:overview")).content.decode()
        assert "Há uma versão nova do núcleo: 0.7.1." in body
        assert reverse("platform:updates-available") in body
    with subtests.test("one agent call for many pages"):
        client.get(reverse("platform:users"))
        assert double.calls.count("update_status") == 1
    with subtests.test("not on the update pages"):
        body = client.get(reverse("platform:updates-operations")).content.decode()
        assert "Há atualizações disponíveis" not in body
    with subtests.test("check now recomputes"):
        client.post(reverse("platform:updates-available"))
        client.get(reverse("platform:overview"))
        assert double.calls.count("update_status") == 2


def test_banner_counts_apps_with_plural(admin, monkeypatch, notice_cache):
    two = {
        "paths": [
            {
                "component_id": f"org.example.app{n}",
                "latest": "2.0.0",
                "steps": [{"version": "2.0.0", "source_id": "s"}],
            }
            for n in (1, 2)
        ]
    }
    notice_agent(monkeypatch, {**two, "paths": [*two["paths"], STATUS["paths"][0]]})
    client = support.login(admin, password=PASSWORD, csrf=False)
    assert (
        "Há atualizações disponíveis: núcleo 0.7.1 e 2 apps."
        in client.get(reverse("platform:overview")).content.decode()
    )
    from django.core.cache import cache

    cache.clear()
    notice_agent(monkeypatch, {"paths": two["paths"][:1]})
    assert "Há atualizações para 1 app." in client.get(reverse("platform:overview")).content.decode()


def test_overview_says_up_to_date(admin, monkeypatch, notice_cache):
    notice_agent(monkeypatch, {"paths": []})
    client = support.login(admin, password=PASSWORD, csrf=False)
    body = client.get(reverse("platform:overview")).content.decode()
    assert "Tudo atualizado." in body and "Há atualizações" not in body


def test_banner_never_breaks_a_page_when_the_agent_is_down(admin, monkeypatch, notice_cache):
    from skeleton.system.releases import DeploymentUnavailable

    notice_agent(monkeypatch, DeploymentUnavailable("down"))
    client = support.login(admin, password=PASSWORD, csrf=False)
    assert client.get(reverse("platform:overview")).status_code == 200
    assert client.get(reverse("platform:users")).status_code == 200


def test_no_banner_and_no_agent_call_without_the_capability(admin, monkeypatch, notice_cache):
    double = notice_agent(monkeypatch)
    with system_context(reason="test:banner-auditor"):
        auditor = create_identity(email="banner-auditor@example.test", password=PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=auditor, email=auditor.email, verified=True, primary=True)
    with request_context(actor=admin, request_id=uuid4(), source="test:banner"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    client = support.login(auditor, password=PASSWORD, csrf=False)
    assert "Há atualizações" not in client.get(reverse("platform:audit")).content.decode()
    anonymous = Client()
    anonymous.get(reverse("account_login"))
    admin_client = support.login(admin, password=PASSWORD, csrf=False)
    admin_client.get("/me/")  # Outside /platform/: never the agent.
    assert double.calls == []


def test_informative_banner_without_agent(admin, settings, notice_cache):
    from skeleton.system.jobs import GITHUB_NOTICE_KEY

    settings.SKELETON_AGENT_SOCKET = None
    notice_cache.set(
        GITHUB_NOTICE_KEY,
        {
            "latest": "0.7.1",
            "newer": True,
            "url": "https://github.com/acme/skeleton/releases/tag/v0.7.1",
            "checked_at": "x",
        },
    )
    client = support.login(admin, password=PASSWORD, csrf=False)
    for name in ("platform:overview", "platform:updates"):
        body = client.get(reverse(name)).content.decode()
        assert "Há uma versão nova do Skeleton (0.7.1)." in body, name
        assert 'href="https://github.com/acme/skeleton/releases/tag/v0.7.1" rel="noopener noreferrer"' in body
        assert "Ver o passo a passo" not in body
    with system_context(reason="test:informative-auditor"):
        auditor = create_identity(email="informative-auditor@example.test", password=PASSWORD)
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(user=auditor, email=auditor.email, verified=True, primary=True)
    with request_context(actor=admin, request_id=uuid4(), source="test:informative"):
        grant_platform_role(actor=admin, user=auditor, role="auditor")
    other = support.login(auditor, password=PASSWORD, csrf=False)
    assert "versão nova do Skeleton" not in other.get(reverse("platform:audit")).content.decode()
